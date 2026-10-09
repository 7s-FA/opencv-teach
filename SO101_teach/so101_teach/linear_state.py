"""Read-only ROS command-state monitoring; never actuator position feedback."""
from copy import deepcopy
import json
import math
import os
import selectors
import queue
import re
import shlex
import subprocess
import threading
import time

# The Pi publishes the last commanded pulse, not actuator position feedback.
# This helper only subscribes. It cannot start the controller or publish a command.
STATE_READER = '''import json,os,time,uuid
import rclpy
from std_msgs.msg import Int32
rclpy.init()
node=rclpy.create_node("so101_preview_state_"+uuid.uuid4().hex[:10])
values=[]
sub=node.create_subscription(Int32,TOPIC,lambda message:values.append(message.data),10)
deadline=time.monotonic()+4.0
try:
 while not values and time.monotonic()<deadline:rclpy.spin_once(node,timeout_sec=.2)
 print("SO101_LINEAR_STATE="+json.dumps({"pulse_us":values[-1] if values else None}))
finally:
 node.destroy_node()
 rclpy.shutdown()
'''


def decode_state(pulse):
    if pulse is None or pulse == -1:
        return {'known':False,'reason':'제어기 상태 미확인','measured':False}
    if type(pulse) is not int or not 1000 <= pulse <= 2000:
        raise ValueError('리니어 상태 범위 오류')
    return {'known':True,'pulse_us':pulse,'commanded_mm':(pulse-1000)/10.,
            'measured':False,'received_wall_time':time.time()}


def read_state(connection,source):
    from .pi_connection import validate
    from .remote_client import ssh_args
    connection=validate(connection,True)
    environment=source.get('environment_script')
    if not isinstance(environment,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',environment):
        raise ValueError('리니어 환경 설정 경로 오류')
    command='source '+shlex.quote(environment)+' && exec /usr/bin/python3 -'
    script='import os\nTOPIC=os.environ["ARM3_NAMESPACE"].rstrip("/")+"/linear_state"\n'+STATE_READER
    try:
        result=subprocess.run(ssh_args(connection)+['-l',connection['user'],connection['host'],
                              'bash -c '+shlex.quote(command)],input=script,text=True,
                              capture_output=True,timeout=10)
    except subprocess.TimeoutExpired:
        raise ValueError('리니어 상태 확인 시간 초과') from None
    if result.returncode:raise ValueError('Pi 리니어 상태를 읽지 못했습니다.')
    lines=[line for line in result.stdout.splitlines() if line.startswith('SO101_LINEAR_STATE=')]
    if len(lines)!=1:raise ValueError('리니어 상태 응답 오류')
    return decode_state(json.loads(lines[0].split('=',1)[1])['pulse_us'])


STATE_TIMEOUT=1.0
STREAM_TIMEOUT=2.0
STARTUP_TIMEOUT=8.0

STREAM_READER = '''import json,time,uuid
import rclpy
from std_msgs.msg import Int32
rclpy.init()
node=rclpy.create_node("so101_preview_state_"+uuid.uuid4().hex[:10])
value=[None,None]
def receive(message):
 value[:]=[message.data,time.monotonic()]
sub=node.create_subscription(Int32,TOPIC,receive,10)
next_report=0.
try:
 while rclpy.ok():
  rclpy.spin_once(node,timeout_sec=.1)
  now=time.monotonic()
  if now>=next_report:
   print("SO101_LINEAR_STATE="+json.dumps({"pulse_us":value[0],"age_s":None if value[1] is None else now-value[1]}),flush=True)
   next_report=now+.2
finally:
 node.destroy_node()
 rclpy.shutdown()
'''


def stream_command(connection,source):
    from .pi_connection import validate
    from .remote_client import ssh_args
    connection=validate(connection,True)
    environment=source.get('environment_script')
    if not isinstance(environment,str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',environment):
        raise ValueError('리니어 환경 설정 경로 오류')
    topic=source.get('topic')
    if topic is not None and (not isinstance(topic,str) or not re.fullmatch(r'/[A-Za-z0-9_/]+',topic)):
        raise ValueError('리니어 상태 토픽 오류')
    prefix='import os\n'
    if 'domain_id' in source:
        domain=source['domain_id']
        if type(domain) is not int or not 0<=domain<=232:raise ValueError('리니어 ROS 도메인 오류')
        prefix+=f'os.environ["ROS_DOMAIN_ID"]={str(domain)!r}\n'
    prefix+=f'TOPIC={topic!r}\n' if topic else 'TOPIC=os.environ["ARM3_NAMESPACE"].rstrip("/")+"/linear_state"\n'
    command='source '+shlex.quote(environment)+' && exec /usr/bin/python3 -u -'
    args=ssh_args(connection)+['-l',connection['user'],connection['host'],'bash -c '+shlex.quote(command)]
    return args,prefix+STREAM_READER


def decode_report(report):
    age=report.get('age_s')
    if age is None:return {'known':False,'connected':False,'measured':False,'reason':'리니어 상태 수신 대기'}
    if type(age) not in (int,float) or not math.isfinite(age) or age<0:raise ValueError('리니어 상태 수신 시각 오류')
    if age>STATE_TIMEOUT:return {'known':False,'connected':False,'measured':False,'reason':'리니어 상태 수신 끊김'}
    return {**decode_state(report.get('pulse_us')),'connected':True}


class StateStream(threading.Thread):
    """One SSH subscriber, bounded latest-only mailbox and interruptible reconnect."""
    def __init__(self,connection,source):
        super().__init__(daemon=True,name='linear-state-stream')
        self.connection=deepcopy(connection);self.source=deepcopy(source)
        self.results=queue.Queue(maxsize=1);self.stop=threading.Event();self.process=None
        self.last_state=None;self.last_received_at=None
    def publish(self,state):
        signature={k:v for k,v in state.items() if k!='received_wall_time'}
        if signature==self.last_state:return
        self.last_state=signature
        try:self.results.get_nowait()
        except queue.Empty:pass
        self.results.put_nowait(state)
    def close(self):
        self.stop.set()
    def read_stream(self):
        args,script=stream_command(self.connection,self.source)
        process=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        self.process=process
        try:
            process.stdin.write(script.encode());process.stdin.close()
            buffer=b'';deadline=time.monotonic()+STARTUP_TIMEOUT
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                while not self.stop.is_set():
                    if time.monotonic()>deadline:raise TimeoutError('리니어 상태 수신 시간 초과')
                    if not selector.select(.1):continue
                    chunk=os.read(process.stdout.fileno(),4096)
                    if not chunk:raise ConnectionError('리니어 상태 연결 종료')
                    buffer+=chunk
                    if len(buffer)>65536:raise ValueError('리니어 상태 응답 크기 오류')
                    while b'\n' in buffer:
                        line,buffer=buffer.split(b'\n',1)
                        if not line.startswith(b'SO101_LINEAR_STATE='):continue
                        state=decode_report(json.loads(line.split(b'=',1)[1]))
                        self.last_received_at=time.monotonic();deadline=self.last_received_at+STREAM_TIMEOUT
                        self.publish(state)
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=.5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.5)
            for pipe in (process.stdin,process.stdout):
                if pipe:pipe.close()
            self.process=None
    def run(self):
        delay=1.
        while not self.stop.is_set():
            began=time.monotonic()
            try:self.read_stream()
            except Exception as exc:
                if not self.stop.is_set():self.publish({'known':False,'connected':False,'measured':False,'reason':str(exc)})
            if self.stop.wait(delay):break
            delay=1. if time.monotonic()-began>10 else min(5.,delay*2)


def apply_state(placement,state):
    """Keep the saved geometry as an explicit example when feedback is unavailable."""
    value=deepcopy(placement)
    if state.get('known'):
        position=state.get('commanded_mm')
        if type(position) not in (int,float) or not 0 <= position <= 100:
            raise ValueError('리니어 목표 위치 오류')
        value['linear_stage']['stroke_mm']=float(position)
    previous=value['linear_stage'].get('startup_state',{})
    state=deepcopy(state)
    if not state.get('known'):
        last=previous.get('commanded_mm') if previous.get('known') else previous.get('last_commanded_mm')
        if last is not None:state['last_commanded_mm']=last
    value['linear_stage']['startup_state']=state
    return value


def state_label(placement):
    stage=placement.get('linear_stage',{});state=stage.get('startup_state',{})
    if state.get('pending'):return '리니어: 상태 확인 중 · 기준 배치'
    if not state.get('known'):
        status='수신 끊김' if state.get('connected') is False else '명령 기록 없음' if state.get('connected') is True else '상태 미확인'
        last=state.get('last_commanded_mm')
        if last is not None:return '리니어: '+status+' · 마지막 '+direction_label(stage,last)+' 명령'
        return '리니어: '+status+' · 기준 배치'
    return '리니어: '+direction_label(stage,state['commanded_mm'])+' 목표 · 위치 미측정'

def direction_label(stage,mm):
    endpoints=stage.get('endpoint_reference',{})
    name='전진' if mm==endpoints.get('forward',{}).get('commanded_mm',100) else '후진' if mm==endpoints.get('retracted',{}).get('commanded_mm',1.5) else f'{mm:g}mm'
    return name
