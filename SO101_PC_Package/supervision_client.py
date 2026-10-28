"""Persistent request identity and fresh bidirectional supervision on the PC."""
from collections import OrderedDict
import fcntl,json,os,tempfile,threading,time,uuid
from pathlib import Path

LEASE_SECONDS=3.
HEARTBEAT_TOPIC='/workcell/supervision/heartbeat'
STATUS_TOPIC='/workcell/supervision/status'

def valid_token(value):
    return isinstance(value,str) and 0<len(value)<=128


def atomic_record(path,value):
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.request-')
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:Path(name).unlink(missing_ok=True)


class ClientJournal:
    def __init__(self,arm):
        root=Path(os.environ.get('SO101_CLIENT_STATE_DIR',Path.home()/'.local/state/so101-supervision-client'))
        root.mkdir(parents=True,exist_ok=True);self.path=root/(arm+'.json')
        self.guard=(root/(arm+'.lock')).open('a')
        try:fcntl.flock(self.guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.guard.close();raise RuntimeError(arm+': 다른 관제 프로세스가 이 PC의 요청 기록을 사용 중입니다.') from None
        self.value=json.loads(self.path.read_text()) if self.path.exists() else {'client_id':uuid.uuid4().hex,'job':None,'control':None}
        key=self.value.get('client_id','')
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('관제 요청 기록의 client_id 오류')
        self.save()
    def save(self):atomic_record(self.path,self.value)
    def close(self):self.guard.close()


class ClientHealth:
    def __init__(self,node,client_id,*,challenge_sink=None):
        from std_msgs.msg import String
        from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
        self.String=String;self.client_id=client_id;self.lock=threading.RLock()
        self.challenge_sink=challenge_sink;self.pending=OrderedDict()
        self.epoch=None;self.nonce=None;self.last_ack=float('-inf');self.sent=OrderedDict()
        # Challenges and acknowledgments for several clients share these topics.
        # Keep a bounded burst without weakening timestamp/nonce validation.
        qos=QoSProfile(depth=16,reliability=ReliabilityPolicy.BEST_EFFORT,durability=DurabilityPolicy.VOLATILE)
        self.publisher=node.create_publisher(String,HEARTBEAT_TOPIC,qos)
        self.subscription=node.create_subscription(String,STATUS_TOPIC,self.receive,qos)
    def receive(self,message):
        if not isinstance(message.data,str) or len(message.data)>2048:return
        try:value=json.loads(message.data)
        except (ValueError,TypeError,RecursionError):return
        if not isinstance(value,dict) or not all(valid_token(value.get(k)) for k in ('epoch','nonce')):return
        now=time.monotonic()
        with self.lock:
            if value.get('kind')=='challenge':
                epoch=value.get('epoch');nonce=value.get('nonce')
                if not isinstance(epoch,str) or not isinstance(nonce,str):return
                if self.epoch!=epoch:
                    self.sent.clear();self.pending.clear();self.last_ack=float('-inf');self.epoch=epoch
                if self.challenge_sink is not None:
                    if nonce in self.pending or nonce in self.sent:return
                    self.pending[nonce]=now
                    while self.pending and now-next(iter(self.pending.values()))>=1.:self.pending.popitem(last=False)
                    self.challenge_sink(epoch,nonce)
                    return
                self.sent[nonce]=now
                while self.sent and now-next(iter(self.sent.values()))>3.:self.sent.popitem(last=False)
                self.publisher.publish(self.String(data=json.dumps({'client_id':self.client_id,'epoch':epoch,'nonce':nonce})))
            elif value.get('kind')=='ack' and value.get('client_id')==self.client_id and value.get('epoch')==self.epoch:
                issued=self.sent.get(value.get('nonce'))
                if issued is not None and now-issued<1.:
                    self.last_ack=now;self.nonce=value['nonce']
    def acknowledge(self,epoch,nonce):
        """Relay only a fresh response obtained from the active WPF UI thread."""
        with self.lock:
            issued=self.pending.pop(nonce,None);now=time.monotonic()
            if epoch!=self.epoch or issued is None or now-issued>=1.:return False
            self.sent[nonce]=issued
            while len(self.sent)>16:self.sent.popitem(last=False)
            self.publisher.publish(self.String(data=json.dumps({'client_id':self.client_id,'epoch':epoch,'nonce':nonce})))
            return True
    def invalidate(self):
        with self.lock:
            self.pending.clear();self.sent.clear();self.nonce=None;self.last_ack=float('-inf')
    def online(self):
        with self.lock:return time.monotonic()-self.last_ack<LEASE_SECONDS
    def envelope(self,command,request_id,operation='execute'):
        with self.lock:return json.dumps({'version':2,'command':command,'client_id':self.client_id,
            'request_id':request_id,'attempt_id':uuid.uuid4().hex,'epoch':self.epoch,'nonce':self.nonce,'operation':operation})
