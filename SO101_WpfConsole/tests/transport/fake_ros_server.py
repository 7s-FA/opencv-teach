"""Hardware-free ROS peer. Uses the production Pi supervision lease/watchdog.

Refuses production domain 40. Never imports device or episode execution modules.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

if os.environ.get('ROS_DOMAIN_ID')=='40' or os.environ.get('ROS_AUTOMATIC_DISCOVERY_RANGE')!='LOCALHOST':
    raise SystemExit('Fake peer requires an isolated ROS domain and LOCALHOST discovery')
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root.parent/'SO101_teach/integration'))
import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer,GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from host_pkg.action import Arm
from supervision_gate import SupervisionGate
from supervision_session import SupervisionHealth,RequestLedger,decode_goal,atomic_record

parser=argparse.ArgumentParser();parser.add_argument('--state',type=Path,required=True);parser.add_argument('--duration',type=float,default=12.)
args=parser.parse_args();args.state.mkdir(parents=True,exist_ok=True)
gate=SupervisionGate();log_lock=threading.Lock();jobs={};stop_generation=0

def record(kind,**values):
    with log_lock:
        with (args.state/'events.jsonl').open('a') as stream:stream.write(json.dumps(dict(at=time.time(),kind=kind,**values))+'\n')
def state(*unused,**kw):
    ticket=gate.active
    return dict(job_id=ticket.job_id if ticket else None,stop_generation=gate.generation if ticket else None)

class Server(Node):
    def __init__(self,arm):
        super().__init__('wpf_lab_'+arm);self.arm=arm;self.runtime_root=args.state
        self.protocol=type('Protocol',(),{'control_state':staticmethod(state)})()
        if not hasattr(gate,'health'):
            gate.health=SupervisionHealth(self,gate,lambda text:record('supervision',detail=text))
            gate.ledger=RequestLedger(args.state,gate.health.epoch)
        self.health=gate.health;self.ledger=gate.ledger
        self.server=ActionServer(self,Arm,'/'+arm+'/data',goal_callback=self.accept,execute_callback=self.execute,callback_group=ReentrantCallbackGroup())
    def accept(self,request):
        try:meta=decode_goal(request.command)
        except Exception:return GoalResponse.REJECT
        if meta.get('operation')!='query' and meta['command']!='EMER_STOP' and not self.health.accepts(meta):
            record('reject',command=meta['command'],reason='lease');return GoalResponse.REJECT
        return GoalResponse.ACCEPT
    def execute(self,handle):
        meta=decode_goal(handle.request.command);key=meta['request_id'];command=meta['command']
        if meta.get('operation')=='query' or self.ledger.lookup(meta,self.arm) is not None:
            if self.ledger.lookup(meta,self.arm) is None:
                self.ledger.accept(meta,self.arm);self.ledger.finish(meta,self.arm,False,'NOT_FOUND')
            success,message=self.ledger.response(meta,self.arm);record('query',request_id=key,command=command,message=message)
            handle.succeed();return Arm.Result(success=success,message=message)
        self.ledger.accept(meta,self.arm);record('execute',request_id=key,command=command,arm=self.arm)
        success=True;message='IDLE'
        if command in ('EMER_STOP','RESTART','RESET','CLEAR_ERROR'):
            ticket=gate.active
            if command=='EMER_STOP':gate.stop_active()
            elif command=='RESTART':
                if ticket and self.health.control_fence(meta,state()):ticket.stopped=False;ticket.link_lost=False
                else:success=False
            elif ticket:jobs[key if key in jobs else ticket.job_id]['cancel']=True
            time.sleep(.05)
        else:
            try:ticket=gate.reserve(key,meta['client_id']);ticket.started=True
            except RuntimeError:
                self.ledger.finish(meta,self.arm,False,'ERROR');handle.abort();return Arm.Result(success=False,message='ERROR')
            jobs[key]={'cancel':False};elapsed=0.;previous=time.monotonic();last=None
            try:
                while rclpy.ok() and elapsed<args.duration:
                    now=time.monotonic()
                    phase='PAUSED' if ticket.stopped else 'BUILD_DONE' if command.startswith('BUILD_LOAD') and elapsed>1. else 'RUNNING'
                    if phase!=last:
                        handle.publish_feedback(Arm.Feedback(message=phase));record('phase',request_id=key,phase=phase);last=phase
                    if not ticket.stopped:elapsed+=now-previous
                    if jobs[key]['cancel']:success=False;break
                    signal=args.state/'finish.json'
                    if signal.exists():
                        value=json.loads(signal.read_text());signal.unlink();success=value.get('success',True);break
                    previous=now;time.sleep(.05)
                success=success and rclpy.ok()
            finally:gate.retire(ticket);jobs.pop(key,None)
        message='IDLE' if success else 'ERROR';self.ledger.finish(meta,self.arm,success,message)
        record('result',request_id=key,command=command,success=success,message=message)
        if success:handle.succeed()
        else:handle.abort()
        return Arm.Result(success=success,message=message)

rclpy.init();nodes=[Server('arm2'),Server('arm3')];executor=MultiThreadedExecutor(num_threads=8)
for node in nodes:executor.add_node(node)
record('ready',domain=os.environ['ROS_DOMAIN_ID'])
try:executor.spin()
except KeyboardInterrupt:pass
finally:
    gate.health.close();rclpy.try_shutdown();executor.shutdown(timeout_sec=3)
    for node in nodes:node.destroy_node()
