"""SO-101 Action client with bounded waits and durable business request IDs."""
from functools import partial
from copy import deepcopy
import threading,time,uuid
from types import SimpleNamespace
from action_msgs.msg import GoalStatus
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.task import Future
from host_pkg.action import Arm
from console_ui import PromptLogger
from console_text import state_name, arm_name
from supervision_client import ClientJournal,ClientHealth
from pc_compute_client import PcComputePreparation,PC_COMMANDS

try:
    from shared_data import shared_memory
except ModuleNotFoundError as exc:
    if exc.name!='shared_data':raise
    shared_memory=SimpleNamespace()

COMMANDS={
    'arm2':{'BUILD_A','BUILD_B','BUILD_LOAD_A','BUILD_LOAD_B','PC_BUILD_LOAD_A','PC_BUILD_LOAD_B','BUILD_SLIDE','PREPARE_HOME'},
    'arm3':{'LOAD_A','LOAD_B','LOAD_SLIDE'},
}
CONTROLS={'EMER_STOP','RESTART','RESET','CLEAR_ERROR'}
ALIASES={'ESTOP':'EMER_STOP'}


class ArmActionClient(Node):
    def __init__(self,arm,*,monitor_jobs=True,logger_factory=None,challenge_sink=None,journal_writer=None):
        super().__init__(arm+'_client',namespace='host')
        self.console_logger=(logger_factory or PromptLogger)(self.get_logger());self.arm=arm
        self._client=ActionClient(self,Arm,f'/{arm}/data');self._lock=threading.RLock()
        self.journal=ClientJournal(arm)
        self.health=ClientHealth(self,self.journal.value['client_id']) if challenge_sink is None else ClientHealth(self,self.journal.value['client_id'],challenge_sink=challenge_sink)
        self.storage_error=None;self._journal_dirty=False;self._next_save=0.
        self.pc_compute=PcComputePreparation(self,Future)
        self.journal_writer=journal_writer;self._save_serial=0;self._save_completed=0
        self.monitor_jobs=monitor_jobs;self._next_query=time.monotonic()+1.;self._requests=[];self._admissions={}
        job=self.journal.value.get('job')
        self.busy=bool(job and job['state'] in ('pending','unknown'))
        self.feedback='UNKNOWN';self.communication='CONNECTING';self.last_result=None;self.last_control_result=None
        self._write_shared(False,end_flag=False,success=False,end_message='',feedback_msg='UNKNOWN',communication='CONNECTING')
        self._write_shared(True,end_flag=False,success=False,end_message='')
        self._timer=self.create_timer(.1,self.poll)

    def _write_shared(self,control,**values):
        prefix=self.arm+('_control_' if control else '_')
        for key,value in values.items():setattr(shared_memory,prefix+key,value)

    def _save_journal(self,*,on_saved=None):
        if self.journal_writer is not None:
            self._save_serial+=1;serial=self._save_serial
            def complete(error):
                if serial>=self._save_completed:
                    self._save_completed=serial
                    if error is not None:self._storage_failed(error)
                    else:
                        self.storage_error=None;self._journal_dirty=False
                        self._write_shared(False,storage_error='')
                if on_saved is not None:on_saved(error is None)
            self.journal_writer(self.journal.path,deepcopy(self.journal.value),complete)
            return True
        try:self.journal.save()
        except OSError as exc:
            self._storage_failed(exc)
            if on_saved is not None:on_saved(False)
            return False
        self.storage_error=None;self._journal_dirty=False;self._write_shared(False,storage_error='')
        if on_saved is not None:on_saved(True)
        return True

    def _storage_failed(self,error):
        error=str(error)
        if error!=self.storage_error:self.console_logger.error('관제 기록 저장 실패: '+error)
        self.storage_error=error;self._journal_dirty=True;self._next_save=time.monotonic()+1.
        self._write_shared(False,storage_error=error)

    def send_goal(self,command,*,request_id=None,dispatch_guard=None,envelope=None):
        command=ALIASES.get(command.strip().upper(),command.strip().upper())
        with self._lock:
            return self._admit_goal(command,request_id,dispatch_guard,envelope,prepare_pc=True)

    def _send_prepared_goal(self,command,request_id,dispatch_guard):
        """Continue a prepared intent through the same admission checks once.

        Preparation owns no dispatch authority: recheck the journal and health,
        then bind a fresh envelope only while the original guard remains valid.
        """
        with self._lock:
            return self._admit_goal(command,request_id,dispatch_guard,None,prepare_pc=False)

    def _admit_goal(self,command,request_id,dispatch_guard,envelope,*,prepare_pc):
        control=command in CONTROLS
        if command in ('EMER_STOP','RESET','CLEAR_ERROR'):self.pc_compute.cancel()
        repeated=self.pc_compute.repeat(command,request_id)
        if repeated is not None:return repeated
        if command not in COMMANDS[self.arm] and not control:return self._reject_local(command,'이 팔에서 지원하지 않는 명령')
        if not control:
            previous=self.journal.value.get('job')
            if self.busy:
                if previous and previous['command']==command and (request_id is None or request_id==previous['request_id']):return self.retry_last()
                return self._reject_local(command,'이전 업무의 완료 여부를 먼저 확인하세요. RETRY로 같은 업무를 조회할 수 있습니다.')
        if control and command!='EMER_STOP':
            previous=self.journal.value.get('control')
            if previous and previous['state'] in ('pending','unknown') and previous['command']==command and (request_id is None or request_id==previous['request_id']):
                return self.retry_last(control=True)
        if request_id is None:request_id=uuid.uuid4().hex
        if not isinstance(request_id,str) or len(request_id)!=32 or any(c not in '0123456789abcdef' for c in request_id):return self._reject_local(command,'업무 ID는 32자리 소문자 16진수여야 합니다.')
        if command!='EMER_STOP' and not self.health.online():return self._reject_local(command,'통신 끊김: 양방향 관제 연결 확인 필요')
        if prepare_pc and command in PC_COMMANDS:
            return self.pc_compute.begin(command,request_id,dispatch_guard)
        return self._record_and_submit(command,request_id,control,dispatch_guard,envelope)

    def _record_and_submit(self,command,request_id,control,dispatch_guard,envelope):
        # A fresh nonce after fsync must never renew an old motion/control
        # intent across a Pi restart, local stop, or supervision recovery.
        if command!='EMER_STOP' and envelope is None:envelope=self.health.envelope(command,request_id,'execute')
        slot='control' if control else 'job'
        previous_record=self.journal.value.get(slot)
        self.journal.value[slot]={'request_id':request_id,'command':command,'state':'pending','created_at':time.time()}
        if command in PC_COMMANDS:self.journal.value[slot].update(self.pc_compute.metadata(request_id))
        if command=='EMER_STOP':
            # Dispatch the urgent stop before any possibly failing or slow disk write.
            future=self._submit(command,request_id,control,'execute');self._save_journal();return future
        if self.journal_writer is not None:
            # Only the bridge uses asynchronous storage. The CLI keeps its original
            # synchronous path. All completion callbacks return to the ROS executor.
            waiting=Future()
            self._admissions[request_id]=waiting
            if not control:self.busy=True;self.last_result=None
            def persisted(success):
                self._admissions.pop(request_id,None)
                current=self.journal.value.get(slot)
                allowed=current is not None and current['request_id']==request_id
                try:allowed=allowed and self.health.online() and (dispatch_guard is None or dispatch_guard())
                except Exception:allowed=False
                if not success or not allowed:
                    if current and current['request_id']==request_id:
                        current['state']='unknown';current['message']='UNKNOWN'
                    waiting.set_result(SimpleNamespace(command=command,success=False,message='UNKNOWN',
                        detail='기록 저장 실패 또는 연결/정지 상태 변경 · 실행하지 않고 동일 ID 조회 대기',request_id=request_id))
                    return
                sent=self._submit(command,request_id,control,'execute',envelope=envelope)
                sent.add_done_callback(lambda done:waiting.set_result(done.result()))
            self._save_journal(on_saved=persisted)
            return waiting
        if not self._save_journal():
            self.journal.value[slot]=previous_record
            return self._reject_local(command,'관제 기록 저장 실패: 실행 명령을 보내지 않았습니다.')
        if not control:self.busy=True;self.last_result=None
        return self._submit(command,request_id,control,'execute',envelope=envelope)

    def retry_last(self,*,control=False):
        with self._lock:
            if not control and self.pc_compute.pending is not None:return self.pc_compute.pending['future']
            record=self.journal.value.get('control' if control else 'job')
            if not record:return self._reject_local('RETRY','조회할 업무 기록이 없습니다.')
            if record['request_id'] in self._admissions:return self._admissions[record['request_id']]
            for request in self._requests:
                if request['request_id']==record['request_id'] and not request['future'].done():
                    self.console_logger.request(self.arm,record['command'],record['request_id'],'처리 결과 대기','기존 요청의 결과를 기다리는 중')
                    return request['future']
            if not self.health.online():return self._reject_local('RETRY','통신 끊김: 연결 복구 후 조회하세요.')
            return self._submit(record['command'],record['request_id'],control,'query')

    def query_request(self,command,request_id,*,control=False):
        """Read an archived business ID without replacing either current journal slot."""
        with self._lock:
            if self.pc_compute.pending is not None and self.pc_compute.pending['key']==request_id:return self.pc_compute.pending['future']
            if request_id in self._admissions:return self._admissions[request_id]
            for request in self._requests:
                if request['request_id']==request_id and not request['future'].done():return request['future']
            if not self.health.online():return self._reject_local('RETRY','통신 끊김: 연결 복구 후 조회하세요.')
            return self._submit(command,request_id,control,'query')

    def _submit(self,command,key,control,operation,*,envelope=None):
        future=Future();now=time.monotonic()
        request={'command':command,'request_id':key,'control':control,'operation':operation,'future':future,
                 'accepted':False,'expires':now+5.,'expired':False,'transports':[]}
        self._requests.append(request)
        self._write_shared(control,end_flag=False,success=False,end_message='',request_id=key)
        if not self._client.server_is_ready():
            self._finish(request,False,'UNKNOWN','서버 연결 확인 실패',definitive=False);return future
        goal=Arm.Goal();goal.command=envelope if envelope is not None else self.health.envelope(command,key,operation)
        self.console_logger.request(self.arm,command,key,'실행 요청' if operation=='execute' else '기존 요청 상태 조회')
        try:
            transport=self._client.send_goal_async(goal,feedback_callback=partial(self.feedback_callback,request=request))
            request['transports'].append(transport)
            transport.add_done_callback(partial(self.goal_response_callback,request=request))
        except Exception as exc:self._finish(request,False,'UNKNOWN','전송 결과 미확인: '+str(exc),definitive=False)
        return future

    def _reject_local(self,command,reason):
        future=Future();self.console_logger.error(f'{arm_name(self.arm)} {command}: {reason}')
        future.set_result(SimpleNamespace(command=command,success=False,message='ERROR',detail=reason,request_id=None))
        return future

    def feedback_callback(self,feedback_data,*,request):
        with self._lock:
            if request['expired'] or request['future'].done() or not self.health.online():return
            message=feedback_data.feedback.message
            if message not in ('IDLE','RUNNING','PAUSED','ERROR','BUILD_DONE'):return
            self._write_shared(request['control'],feedback_msg=message)
            if not request['control']:self.feedback=message
            self.console_logger.request(self.arm,request['command'],request['request_id'],state_name(message))

    def goal_response_callback(self,future,*,request):
        if request['expired']:return
        try:
            handle=future.result()
            if not handle.accepted:
                if request['operation']=='query':request['stop_query']=True
                # Rejection cannot prove a previously sent business request did not run.
                self._finish(request,False,'UNKNOWN','요청 거절 · 동일 업무 조회로 실행 여부 확인 필요',definitive=False);return
            request['accepted']=True
            request['expires']=time.monotonic()+(10. if request['operation']=='query' or request['command'] in ('EMER_STOP','CLEAR_ERROR') else 330. if request['command'] in ('RESET','RESTART') else 600.)
            result=handle.get_result_async();request['transports'].append(result)
            result.add_done_callback(partial(self.get_result_callback,request=request))
        except Exception as exc:self._finish(request,False,'UNKNOWN','접수 결과 미확인: '+str(exc),definitive=False)

    def get_result_callback(self,future,*,request):
        if request['expired']:return
        try:
            response=future.result();message=response.result.message
            success=response.status==GoalStatus.STATUS_SUCCEEDED and response.result.success and message=='IDLE'
            if message=='STORAGE_PENDING':
                self._finish(request,False,'UNKNOWN','Pi 결과 기록 저장 대기 · 같은 업무 ID로 재조회',definitive=False)
            elif message in ('PENDING','UNKNOWN'):
                if message=='UNKNOWN' and request['operation']=='query':request['stop_query']=True
                self._finish(request,False,message,'동일 업무 상태 조회',definitive=False)
            elif success or (response.status in (GoalStatus.STATUS_SUCCEEDED,GoalStatus.STATUS_ABORTED)
                             and not response.result.success and message in ('ERROR','NOT_FOUND','CLEARED')):
                self._finish(request,success,'IDLE' if success else message,'실행부 확인 결과')
            else:
                self._finish(request,False,'UNKNOWN','유효한 완료 결과 없음 · 같은 업무 ID로 재조회',definitive=False)
        except Exception as exc:self._finish(request,False,'UNKNOWN','완료 결과 미확인: '+str(exc),definitive=False)

    def _finish(self,request,success,message,detail,*,definitive=True):
        with self._lock:
            if request['future'].done():return
            if definitive and not request['control']:self.pc_compute.finish(request['request_id'])
            request['expired']=not definitive
            slot='control' if request['control'] else 'job';saved=self.journal.value.get(slot)
            current=saved is not None and saved['request_id']==request['request_id']
            if current:
                saved['state']=('completed' if success else 'failed') if definitive else ('pending' if message=='PENDING' else 'unknown')
                saved['message']=message;saved['auto_query']=not request.get('stop_query',False);self._save_journal()
                if not request['control']:
                    self.busy=not definitive;self.feedback=message if self.health.online() else 'COMM_LOST'
            result=SimpleNamespace(command=request['command'],success=bool(success),message=message,detail=detail,request_id=request['request_id'],accepted=request['accepted'])
            if current:
                self._write_shared(request['control'],end_flag=True,success=bool(success),end_message=message)
                if request['control']:self.last_control_result=result
                else:self.last_result=result;self._write_shared(False,feedback_msg=self.feedback)
            if request in self._requests:self._requests.remove(request)
            self.console_logger.request(self.arm,request['command'],request['request_id'],'완료' if success else state_name(message),detail)
            request['future'].set_result(result)
            if not definitive:
                for transport in request['transports']:
                    if not transport.done():transport.cancel()

    def poll(self):
        with self._lock:
            self.pc_compute.poll()
            online=self.health.online();communication='ONLINE' if online else 'COMM_LOST'
            if communication!=self.communication:
                self.communication=communication;self._write_shared(False,communication=communication)
                self.console_logger.connection(self.arm,communication)
                if not online:self.feedback='COMM_LOST';self._write_shared(False,feedback_msg='COMM_LOST')
            now=time.monotonic()
            for request in list(self._requests):
                if not online or now>=request['expires']:
                    self._finish(request,False,'UNKNOWN','통신 단절 또는 응답 제한시간 초과 · 자동 재실행하지 않음',definitive=False)
            if self._journal_dirty and now>=self._next_save:self._save_journal()
            job=self.journal.value.get('job')
            if self.monitor_jobs and online and self.busy and job and job['state'] in ('pending','unknown') and job.get('auto_query',True) and not self._requests and now>=self._next_query:
                self._next_query=now+1.;self.retry_last()

    def destroy_node(self):
        self.pc_compute.close()
        self.journal.close()
        return super().destroy_node()


class Arm2ActionClient(ArmActionClient):
    def __init__(self,**kwargs):super().__init__('arm2',**kwargs)


class Arm3ActionClient(ArmActionClient):
    def __init__(self,**kwargs):super().__init__('arm3',**kwargs)
