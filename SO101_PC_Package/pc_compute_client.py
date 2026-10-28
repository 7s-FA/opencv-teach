"""Async preparation for the two explicit PC commands; ordinary jobs never import GPU code."""
from pathlib import Path
import queue
import sys
import threading
import time
from types import SimpleNamespace

PC_COMMANDS={'PC_BUILD_LOAD_A','PC_BUILD_LOAD_B'}

class PcComputePreparation:
    def __init__(self,owner,future_factory,*,broker_factory=None,configuration=None):
        self.owner=owner;self.future_factory=future_factory;self.broker_factory=broker_factory;self.configuration=configuration
        self.pending=None;self.brokers={};self.started={};self.prepare_seconds={};self.events=queue.Queue();self.stop_serial=0;self.closed=False
    def initialize(self):
        if self.broker_factory is not None:return
        root=Path(__file__).resolve().parents[1]/'SO101_teach'
        if not (root/'so101_teach/pc_compute_broker.py').is_file():raise ValueError('PC 연산 모듈 경로를 찾을 수 없습니다.')
        if str(root) not in sys.path:sys.path.insert(0,str(root))
        from so101_teach.pc_compute_broker import ComputeBroker,configuration
        self.broker_factory=ComputeBroker;self.configuration=configuration
    def repeat(self,command,key):
        p=self.pending
        if p is not None and p['command']==command and (key is None or key==p['key']):return p['future']
        return None
    def begin(self,command,key,guard):
        try:
            self.initialize();config=self.configuration()
        except Exception as exc:return self.owner._reject_local(command,str(exc))
        future=self.future_factory();cancel=threading.Event()
        self.started[key]=(time.monotonic(),time.time())
        p={'command':command,'key':key,'guard':guard,'epoch':getattr(self.owner.health,'epoch',None),'stop_serial':self.stop_serial,
            'deadline':time.monotonic()+75.,'cancel':cancel,'future':future}
        self.pending=p;self.owner.busy=True;self.owner.feedback='PREPARING_PC'
        self.owner.console_logger.request(self.owner.arm,command,key,'PC GPU·Pi 연결 준비','기존 저장 레시피 사용 · 준비 중 이동 없음')
        def prepare():
            broker=None
            try:
                broker=self.broker_factory(key,command,cancel=cancel,config=config).start()
                if cancel.is_set() or self.closed:broker.close();raise RuntimeError('PC 준비 취소됨')
                self.events.put((p,broker,None))
            except Exception as exc:
                if broker is not None:broker.close()
                self.events.put((p,None,str(exc)))
        threading.Thread(target=prepare,daemon=True,name='pc-compute-prepare').start()
        return future
    def cancel(self):
        self.stop_serial+=1
        if self.pending is not None:self.pending['cancel'].set()
    @staticmethod
    def close_broker(broker):threading.Thread(target=broker.close,daemon=True,name='pc-compute-close').start()
    def metadata(self,key):
        if key not in self.started:return {}
        return {'created_at':self.started[key][1],'pc_preparation_seconds':self.prepare_seconds.get(key)}
    def finish(self,key):
        began=self.started.pop(key,None)
        if began is not None:
            saved=self.owner.journal.value.get('job')
            if saved is not None and saved['request_id']==key:
                saved['pc_command_total_seconds']=time.monotonic()-began[0]
                saved['pc_preparation_seconds']=self.prepare_seconds.get(key)
        self.prepare_seconds.pop(key,None)
        broker=self.brokers.pop(key,None)
        if broker is not None:self.close_broker(broker)
    def preparation_valid(self,item):
        valid=item is self.pending and not item['cancel'].is_set() and item['stop_serial']==self.stop_serial and not self.closed
        try:
            return (valid and time.monotonic()<item['deadline'] and self.owner.health.online()
                    and getattr(self.owner.health,'epoch',None)==item['epoch'] and (item['guard'] is None or item['guard']()))
        except Exception:return False

    def poll(self):
        p=self.pending
        if p is not None and (not self.owner.health.online() or getattr(self.owner.health,'epoch',None)!=p['epoch'] or time.monotonic()>p['deadline']):p['cancel'].set()
        while True:
            try:item,broker,error=self.events.get_nowait()
            except queue.Empty:break
            valid=self.preparation_valid(item)
            if item is self.pending:self.pending=None;self.owner.busy=False
            if error or not valid:
                if broker is not None:self.close_broker(broker)
                self.started.pop(item['key'],None)
                if not item['future'].done():item['future'].set_result(SimpleNamespace(command=item['command'],success=False,message='ERROR',detail=error or 'PC 준비 중 정지·연결 변경 · 명령을 보내지 않았습니다.',request_id=item['key']))
                continue
            self.brokers[item['key']]=broker;self.prepare_seconds[item['key']]=time.monotonic()-self.started[item['key']][0]
            # Only this unchanged intent may receive a fresh short-lived envelope.
            # A stop, lost supervision, Pi restart or expired UI guard cannot resume it.
            guard=lambda item=item:(item['stop_serial']==self.stop_serial and self.owner.health.online()
                and getattr(self.owner.health,'epoch',None)==item['epoch'] and (item['guard'] is None or item['guard']()))
            sent=self.owner._send_prepared_goal(item['command'],item['key'],guard)
            def completed(done,item=item):
                result=done.result()
                if result.message not in ('UNKNOWN','PENDING'):self.finish(item['key'])
                if not item['future'].done():item['future'].set_result(result)
            sent.add_done_callback(completed)
    def close(self):
        self.closed=True;self.cancel()
        for key in list(self.brokers):self.finish(key)
        while True:
            try:_,broker,_=self.events.get_nowait()
            except queue.Empty:break
            if broker is not None:self.close_broker(broker)
