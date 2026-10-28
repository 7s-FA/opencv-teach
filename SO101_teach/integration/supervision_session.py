"""Fresh bidirectional leases and a crash-safe application request ledger."""
from collections import OrderedDict
import json
import errno
import os
import queue
from pathlib import Path
import tempfile
import threading
import time
import uuid

LEASE_SECONDS=3.0
HEARTBEAT_TOPIC='/workcell/supervision/heartbeat'
STATUS_TOPIC='/workcell/supervision/status'


def valid_id(value):
    return isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)


def valid_token(value):
    return isinstance(value,str) and 0<len(value)<=128


def decode_goal(text):
    if not isinstance(text,str) or len(text)>16384:raise ValueError('INVALID_MESSAGE')
    if text=='EMER_STOP':return {'command':text,'attempt_id':'legacy-emergency','legacy':True}
    value=json.loads(text)
    if not isinstance(value,dict) or value.get('version')!=2:raise ValueError('SUPERVISION_V2_REQUIRED')
    if not all(valid_id(value.get(key)) for key in ('client_id','request_id','attempt_id')):raise ValueError('INVALID_REQUEST_ID')
    if not isinstance(value.get('command'),str):raise ValueError('INVALID_COMMAND')
    if value.get('operation','execute') not in ('execute','query'):raise ValueError('INVALID_OPERATION')
    for key in ('epoch','nonce'):
        if value.get(key) is not None and not valid_token(value[key]):raise ValueError('INVALID_LEASE_TOKEN')
    return value


def atomic_record(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.ledger-')
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)


class RequestLedger:
    def __init__(self,root,epoch):
        self.folder=Path(root)/'data/supervision-requests';self.epoch=epoch
        self.lock=threading.RLock();self.pending_results={}
    def path(self,meta):return self.folder/(meta['request_id']+'.json')
    @staticmethod
    def read_record(path):
        # A syntactically valid JSON document is not necessarily a valid result.
        # Treat corruption as unavailable storage, never absence or truthy success.
        try:
            record=json.loads(path.read_text())
            valid=(isinstance(record,dict)
                   and all(valid_id(record.get(key)) for key in ('request_id','client_id'))
                   and record['request_id']==path.stem
                   and isinstance(record.get('command'),str) and bool(record['command'])
                   and record.get('arm') in ('arm2','arm3')
                   and isinstance(record.get('epoch'),str) and bool(record['epoch']))
            if not valid:raise ValueError('invalid request identity')
            state=record.get('state')
            if state=='accepted':
                valid='success' not in record and 'message' not in record
            else:
                expected={'completed':(True,('IDLE',)),
                          'failed':(False,('ERROR','NOT_FOUND')),
                          'cleared':(False,('CLEARED',))}.get(state)
                valid=(expected is not None and type(record.get('success')) is bool
                       and record['success'] is expected[0] and record.get('message') in expected[1])
            if not valid:raise ValueError('invalid request outcome')
            return record
        except (ValueError,TypeError,RecursionError) as exc:
            raise OSError(errno.EIO,'요청 결과 기록 형식 오류: '+path.name) from exc
    def lookup(self,meta,arm):
        with self.lock:
            path=self.path(meta)
            if not path.exists():
                pending=self.pending_results.get(meta['request_id'])
                if pending is None:return None
                saved,saved_arm,_,_=pending
                record={key:saved[key] for key in ('request_id','client_id','command')}
                record.update(arm=saved_arm,epoch=self.epoch,state='accepted')
            else:record=self.read_record(path)
            if any(record.get(k)!=meta.get(k) for k in ('request_id','client_id','command')) or record.get('arm')!=arm:
                raise ValueError('REQUEST_ID_CONFLICT')
            return record
    def accept(self,meta,arm):
        with self.lock:
            if self.lookup(meta,arm) is not None:raise ValueError('REQUEST_ALREADY_EXISTS')
            record={key:meta[key] for key in ('request_id','client_id','command')}
            record.update(arm=arm,epoch=self.epoch,state='accepted',at=time.time())
            try:atomic_record(self.path(meta),record)
            except OSError:
                # No execution permission was granted. A rename may already have
                # committed: retain this rejection even if no file is visible yet.
                self.pending_results[meta['request_id']]=(dict(meta),arm,False,'ERROR')
                raise
    def finish(self,meta,arm,success,message):
        with self.lock:
            # The executor has ended. Retain its verified outcome until durable repair.
            self.pending_results[meta['request_id']]=(dict(meta),arm,bool(success),message)
            record=self.lookup(meta,arm)
            if record is None:raise ValueError('MISSING_REQUEST_LEDGER')
            record.update(state='completed' if success else 'failed',success=bool(success),message=message,finished_at=time.time())
            atomic_record(self.path(meta),record)
            self.pending_results.pop(meta['request_id'],None)
    def response(self,meta,arm):
        with self.lock:
            try:
                record=self.lookup(meta,arm)
                pending=self.pending_results.get(meta['request_id'])
                if pending is not None:
                    self.finish(*pending)
                    record=self.lookup(meta,arm)
            except (OSError,json.JSONDecodeError):return False,'STORAGE_PENDING'
        if record is None:return False,'UNKNOWN'
        if record['state'] in ('completed','failed','cleared'):return record['success'],record['message']
        return False,'PENDING' if record['epoch']==self.epoch else 'UNKNOWN'

    def clear_abandoned(self,client_id):
        """After verified idle, close this client's unresolved pre-restart records.

        CLEARED is operator reconciliation, never a claim of task completion.
        """
        count=0
        with self.lock:
            for path in self.folder.glob('*.json'):
                record=self.read_record(path)
                if record.get('client_id')!=client_id:continue
                pending=self.pending_results.get(record.get('request_id'))
                if pending is not None:
                    self.finish(*pending);count+=1;continue
                if record.get('state')!='accepted' or record.get('epoch')==self.epoch:continue
                record.update(state='cleared',success=False,message='CLEARED',cleared_at=time.time())
                atomic_record(path,record);count+=1
        return count


class SupervisionHealth:
    def __init__(self,node,gate,notice):
        from std_msgs.msg import String
        from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
        self.node=node;self.gate=gate;self.notice=notice;self.String=String
        self.epoch=uuid.uuid4().hex;self.lock=threading.RLock()
        self.challenges=OrderedDict();self.fences={};self.seen={};self.stop=threading.Event();self.notifications=queue.Queue(maxsize=64)
        # Challenges and acknowledgments for several clients share these topics.
        # Keep a bounded burst without weakening timestamp/nonce validation.
        qos=QoSProfile(depth=16,reliability=ReliabilityPolicy.BEST_EFFORT,durability=DurabilityPolicy.VOLATILE)
        self.publisher=node.create_publisher(String,STATUS_TOPIC,qos)
        self.subscription=node.create_subscription(String,HEARTBEAT_TOPIC,self.receive,qos)
        self.timer=node.create_timer(.25,self.challenge)
        self.thread=threading.Thread(target=self.watch,name='supervision-lease',daemon=True);self.thread.start()
    def challenge(self):
        # Logging may block on storage; the independent stop watcher never does.
        try:lost=self.notifications.get_nowait()
        except queue.Empty:pass
        else:
            try:self.notice('COMM_LOST_HOLD_REQUESTED:'+lost)
            except Exception:pass
        try:
            state=self.node.protocol.control_state(self.node.runtime_root,timeout=.1)
            fence=(state.get('job_id'),state.get('stop_generation')) if isinstance(state,dict) and state.get('job_id') else (None,-1)
        except (FileNotFoundError,ConnectionRefusedError):fence=(None,None)
        except (OSError,ValueError):fence=(None,-1)
        with self.gate.lock:
            generation=self.gate.generation
            context_generation=self.gate.context_generation
            # A missing endpoint during startup is not evidence of idle state.
            if fence==(None,None) and self.gate.active is not None:fence=(self.gate.active.job_id,None)
        now=time.monotonic();nonce=uuid.uuid4().hex
        with self.lock:
            self.challenges[nonce]=now;self.fences[nonce]=(generation,*fence,context_generation)
            while self.challenges and now-next(iter(self.challenges.values()))>LEASE_SECONDS:
                key,_=self.challenges.popitem(last=False);self.fences.pop(key,None)
        self.publisher.publish(self.String(data=json.dumps({'kind':'challenge','epoch':self.epoch,'nonce':nonce})))
        try:self.publish_standby_health()
        except Exception:pass  # A stale/missing record expires readiness.
    def receive(self,message):
        if not isinstance(message.data,str) or len(message.data)>2048:return
        try:value=json.loads(message.data)
        except (ValueError,TypeError,RecursionError):return
        if not isinstance(value,dict) or not valid_id(value.get('client_id')):return
        if not all(valid_token(value.get(k)) for k in ('epoch','nonce')):return
        now=time.monotonic()
        with self.lock:
            issued=self.challenges.get(value.get('nonce'))
            if value.get('epoch')!=self.epoch or issued is None or now-issued>1.:return
            # Use challenge issuance, never the arrival time of buffered replies.
            self.seen[value['client_id']]=max(issued,self.seen.get(value['client_id'],0.))
        self.publisher.publish(self.String(data=json.dumps({'kind':'ack','client_id':value['client_id'],'epoch':self.epoch,'nonce':value['nonce']})))
    def fresh(self,client):
        with self.lock:return time.monotonic()-self.seen.get(client,float('-inf'))<LEASE_SECONDS
    def accepts(self,meta):
        if not isinstance(meta,dict) or not valid_id(meta.get('client_id')) or not all(valid_token(meta.get(k)) for k in ('epoch','nonce')):return False
        with self.gate.lock,self.lock:
            issued=self.challenges.get(meta.get('nonce'));fence=self.fences.get(meta.get('nonce'))
            return bool(fence is not None and fence[0]==self.gate.generation and meta.get('epoch')==self.epoch and issued is not None
                        and time.monotonic()-issued<LEASE_SECONDS and self.fresh(meta['client_id']))
    def control_fence(self,meta,state):
        # Bind to executor state observed when the PC's nonce was issued, including local CLI stops.
        with self.gate.lock,self.lock:
            fence=self.fences.get(meta.get('nonce'))
            return bool(fence is not None and fence[1:3]==(state.get('job_id'),state.get('stop_generation'))
                        and fence[3]==self.gate.context_generation)
    def publish_standby_health(self):
        with self.gate.lock,self.lock:
            value={'epoch':self.epoch,'generation':self.gate.generation,'seen':dict(self.seen)}
        # The ROS service imports only integration/ under system Python.
        # Reuse the local standard-library writer; no desktop/runtime import.
        atomic_record(self.node.runtime_root/'data/standby-supervision.json',value)

    def watch(self):
        while not self.stop.wait(.05):
            lost=None
            with self.gate.lock:
                ticket=self.gate.active
                if ticket and getattr(ticket,'client_id',None) and not self.fresh(ticket.client_id) and not getattr(ticket,'link_lost',False):
                    ticket.link_lost=True;self.gate.stop_active();lost=ticket.job_id
            if lost:
                try:self.notifications.put_nowait(lost)
                except queue.Full:pass
    def close(self):
        self.stop.set();self.thread.join(1)
