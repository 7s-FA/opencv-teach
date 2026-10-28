"""Single-executor control service. Receipts never authorize replay after a restart."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from supervision_client import atomic_record

JOBS = {'BUILD_A':'arm2','BUILD_B':'arm2','BUILD_LOAD_A':'arm2','BUILD_LOAD_B':'arm2','PC_BUILD_LOAD_A':'arm2','PC_BUILD_LOAD_B':'arm2',
        'BUILD_SLIDE':'arm2','PREPARE_HOME':'arm2','LOAD_A':'arm3','LOAD_B':'arm3','LOAD_SLIDE':'arm3'}
CONTROLS = {'EMER_STOP','RESTART','RESET','CLEAR_ERROR'}
TERMINAL = {'SUCCEEDED','FAILED','CANCELLED','CLEARED'}
def stamp():return datetime.now(timezone.utc).isoformat()
def valid_id(value):return isinstance(value,str) and len(value)==32 and all(c in '0123456789abcdef' for c in value)
def reply(accepted,code,detail,record=None):
    return dict(protocol_version=1,accepted=accepted,code=code,detail=detail,request=deepcopy(record))

class BridgeError(Exception):
    def __init__(self,code,detail,status=409):self.code,self.detail,self.status=code,detail,status

class BridgeService:
    def __init__(self,arms,root,*,clock=time.monotonic,writer=None):
        self.arms=arms;self.clock=clock;self.instance=uuid.uuid4().hex;self.generation='0'*32
        self.sequence=0;self.active_arm='arm2';self.session_deadline=0.;self.ws_open=False
        self.lease_nonce=None;self.lease_issued=0.;self.challenges={};self.records={};self.dirty=set()
        self.writer=writer;self.saving=set();self.revisions={};self.stop_revision=0;self.admissions=set()
        self.root=Path(root)/'bridge-receipts';self.root.mkdir(parents=True,exist_ok=True)
        # A malformed receipt must fail closed instead of erasing deduplication history.
        for path in self.root.glob('*.json'):
            value=json.loads(path.read_text());key=value['request_id']
            if not valid_id(key) or key!=path.stem or value['command'] not in JOBS.keys()|CONTROLS:
                raise ValueError('중계 요청 기록 형식 오류: '+path.name)
            self.records[key]=value
        for arm in self.arms.values():
            for slot in ('job','control'):
                saved=arm.journal.value.get(slot)
                if saved and saved['request_id'] not in self.records:
                    r=self.make_record(saved['request_id'],saved['command'],arm.arm,'UNKNOWN','기존 관제 기록 · 같은 ID 조회 필요')
                    self.records[r['request_id']]=r;self.save(r)
        jobs=[(arm.arm,arm.journal.value.get('job')) for arm in self.arms.values() if arm.journal.value.get('job')]
        if jobs:self.active_arm=max(jobs,key=lambda entry:float(entry[1].get('created_at',0.)))[0]
        self.refresh_records()

    def save(self,record,on_saved=None):
        key=record['request_id'];version=self.revisions.get(key,0)+1;self.revisions[key]=version
        def completed(error):
            if self.revisions.get(key)==version:
                self.saving.discard(key)
                if error is None:self.dirty.discard(key)
                else:self.dirty.add(key)
            if on_saved is not None:on_saved(error is None)
        if self.writer is not None:
            self.saving.add(key);self.writer(self.root/(key+'.json'),deepcopy(record),completed);return True
        try:atomic_record(self.root/(key+'.json'),record)
        except OSError as exc:completed(exc);return False
        completed(None);return True

    def make_record(self,key,command,arm,state,detail):
        return dict(request_id=key,command=command,arm=arm,kind='CONTROL' if command in CONTROLS else 'JOB',
                    state=state,detail=detail,updated_at=stamp(),result_code=None)

    def expire(self):
        if self.session_deadline and self.clock()>=self.session_deadline:self.close(self.generation)

    def valid_session(self,generation,*,require_ws=False):
        self.expire()
        if generation!=self.generation or not self.session_deadline or (require_ws and not self.ws_open):
            raise BridgeError('SESSION_EXPIRED','관제 연결이 만료되었습니다. 새 연결에서 상태를 확인하세요.')

    def session(self):
        self.expire()
        if self.session_deadline:raise BridgeError('SESSION_IN_USE','다른 관제 화면이 연결되어 있습니다.')
        for arm in self.arms.values():arm.health.invalidate()
        self.generation=uuid.uuid4().hex;self.session_deadline=self.clock()+3.;self.ws_open=False
        self.challenges.clear();self.lease_nonce=None
        return dict(protocol_version=1,session_generation=self.generation,bridge_instance_id=self.instance,demo=False)

    def close(self,generation):
        if generation!=self.generation:return
        self.session_deadline=0.;self.ws_open=False;self.challenges.clear();self.lease_nonce=None
        for arm in self.arms.values():arm.health.invalidate()

    def attach(self,generation):
        self.valid_session(generation)
        if self.ws_open:raise BridgeError('SESSION_IN_USE','이미 연결된 이벤트 채널입니다.')
        self.ws_open=True

    def lease(self,generation):
        self.valid_session(generation,require_ws=True)
        self.lease_nonce=uuid.uuid4().hex;self.lease_issued=self.clock()
        return dict(type='lease',protocol_version=1,bridge_instance_id=self.instance,session_generation=generation,nonce=self.lease_nonce,expires_in_ms=1000)

    def acknowledge(self,generation,value):
        self.valid_session(generation,require_ws=True)
        if value.get('type')=='lease_ack':
            if set(value)=={'type','protocol_version','bridge_instance_id','session_generation','nonce'} and type(value['protocol_version']) is int and value['protocol_version']==1 and value['bridge_instance_id']==self.instance and value.get('session_generation')==generation and value.get('nonce')==self.lease_nonce and self.clock()-self.lease_issued<1.:
                self.session_deadline=self.clock()+3.;self.lease_nonce=None;return True
            return False
        required={'protocol_version','session_generation','bridge_instance_id','arm','epoch','nonce'}
        if set(value)!=required or type(value['protocol_version']) is not int or value['protocol_version']!=1 or value['session_generation']!=generation or value['bridge_instance_id']!=self.instance:return False
        arm=self.arms.get(value['arm']);pending=self.challenges.pop((value['arm'],value['nonce']),None)
        if arm is None or pending is None or pending[0]!=value['epoch'] or self.clock()-pending[1]>=1.:return False
        return arm.health.acknowledge(value['epoch'],value['nonce'])

    def challenge(self,arm,epoch,nonce):
        self.expire()
        if not self.ws_open:return
        now=self.clock()
        self.challenges={k:v for k,v in self.challenges.items() if now-v[1]<1.}
        self.challenges[(arm,nonce)]=(epoch,now)

    def pending_challenges(self,generation):
        self.valid_session(generation,require_ws=True)
        now=self.clock();messages=[]
        for (arm,nonce),(epoch,issued) in self.challenges.items():
            remain=1000-int((now-issued)*1000)
            if remain>0:messages.append(dict(protocol_version=1,session_generation=generation,bridge_instance_id=self.instance,
                                          arm=arm,epoch=epoch,nonce=nonce,expires_in_ms=remain))
        return messages

    def refresh_records(self):
        for arm in self.arms.values():
            for slot in ('job','control'):
                saved=arm.journal.value.get(slot)
                if not saved:continue
                r=self.records.get(saved['request_id'])
                if r is None:continue
                message=saved.get('message')
                state={'completed':'SUCCEEDED','failed':'CLEARED' if message=='CLEARED' else 'FAILED','unknown':'UNKNOWN'}.get(saved['state'],'ACCEPTED')
                if saved['state']=='pending' and message=='PENDING':state='UNKNOWN'
                if saved['state']=='pending' and slot=='job' and arm.feedback in ('RUNNING','BUILD_DONE','PAUSED'):state=arm.feedback
                detail={'SUCCEEDED':'실행부 최종 완료 확인','CLEARED':'실행부 요청 정리 확인','FAILED':'실행부 결과: '+str(message),
                        'UNKNOWN':'결과 미확인 · 동일 요청 ID로 조회','ACCEPTED':'요청 전송 · 실행부 결과 대기',
                        'RUNNING':'작업 진행 중','BUILD_DONE':'조립 완료 · 연속 적재 최종 결과 대기','PAUSED':'작업 보류 · 자동 재개 없음'}[state]
                if (r['state'],r['detail'],r['result_code'])!=(state,detail,message):
                    r.update(state=state,detail=detail,result_code=message,updated_at=stamp());self.save(r)

    def snapshot(self,generation):
        self.valid_session(generation);self.refresh_records();self.sequence+=1
        arms=[]
        for name,arm in self.arms.items():
            slots={slot:deepcopy(self.records.get((arm.journal.value.get(slot) or {}).get('request_id'))) for slot in ('job','control')}
            preparation=getattr(getattr(arm,'pc_compute',None),'pending',None)
            if preparation is not None:
                record=self.records.get(preparation['key'])
                if record is not None:
                    slots['job']={**deepcopy(record),'state':'ACCEPTED','detail':'PC GPU·Pi 연결 준비 · 아직 장치 실행 명령을 보내지 않았습니다.'}
            arms.append(dict(arm=name,connection='ONLINE' if arm.health.online() and arm._client.server_is_ready() else 'COMM_LOST',**slots))
        return dict(protocol_version=1,bridge_instance_id=self.instance,session_generation=generation,sequence=self.sequence,
                    demo=False,bridge_connection='ONLINE',observed_at=stamp(),arms=arms,active_arm=next((a.arm for a in self.arms.values() if a.busy),self.active_arm),
                    storage_error='요청 기록 저장 오류 · 일반 실행 차단' if self.dirty or any(a.storage_error for a in self.arms.values()) else None)

    def admission_rejection(self,command,arm):
        if command in JOBS and any(a.busy for a in self.arms.values()):return reply(False,'NOT_ALLOWED','진행 중인 업무의 최종 결과를 먼저 확인하세요.')
        if command!='EMER_STOP':
            if any(self.records[p]['kind']=='CONTROL' for p in self.admissions):
                return reply(False,'NOT_ALLOWED','이전 제어 요청의 기록 저장과 전송 확인을 기다리세요.')
            if any((a.journal.value.get('control') or {}).get('state') in ('pending','unknown') for a in self.arms.values()):
                return reply(False,'NOT_ALLOWED','이전 제어 요청을 RETRY_CONTROL로 확인하세요.')
            if not arm.health.online():return reply(False,'NOT_ALLOWED','해당 팔의 양방향 관제 연결 확인 필요')
            if self.dirty or any(a.storage_error for a in self.arms.values()):return reply(False,'STORAGE_ERROR','요청 기록 저장 오류 · 일반 실행 차단')
        return None

    def submit(self,value):
        if set(value)!={'protocol_version','session_generation','request_id','command'} or (type(value.get('protocol_version')) is not int or value.get('protocol_version')!=1) or not valid_id(value.get('request_id')):
            raise BridgeError('INVALID_REQUEST','명령 형식 또는 규약 버전 오류',400)
        self.valid_session(value['session_generation'],require_ws=True)
        key,command=value['request_id'],value['command']
        if not isinstance(command,str) or command not in JOBS.keys()|CONTROLS:raise BridgeError('INVALID_COMMAND','지원하지 않는 명령',400)
        previous=self.records.get(key)
        if previous:
            if previous['command']!=command:return reply(False,'ID_CONFLICT','다른 명령에서 사용한 요청 ID입니다.',previous)
            return reply(True,'EXISTING_REQUEST','기존 요청을 반환합니다. 재실행하지 않습니다.',previous)
        name=JOBS.get(command) or next((a.arm for a in self.arms.values() if a.busy),self.active_arm)
        arm=self.arms[name]
        rejection=self.admission_rejection(command,arm)
        if rejection is not None:return rejection
        return self.admit_request(value,arm)

    def admit_request(self,value,arm):
        key,command=value['request_id'],value['command'];name=arm.arm
        r=self.make_record(key,command,name,'UNKNOWN','전송 경계 확인 중 · 자동 재실행하지 않음')
        self.records[key]=r
        if command in ('EMER_STOP','RESET','CLEAR_ERROR'):self.stop_revision+=1
        generation=value['session_generation'];stop_revision=self.stop_revision;dispatch_deadline=self.clock()+(80. if command.startswith('PC_BUILD_LOAD_') else 2.)
        envelope=None if command=='EMER_STOP' else arm.health.envelope(command,key,'execute')
        def still_valid():
            self.valid_session(generation,require_ws=True)
            return self.clock()<dispatch_deadline and self.stop_revision==stop_revision and arm.health.online()
        def dispatch(saved):
            try:allowed=saved and still_valid()
            except BridgeError:allowed=False
            if not allowed:
                r.update(state='UNKNOWN',detail='기록 저장 또는 연결 상태 변경 · 실행하지 않고 동일 ID 조회 대기',updated_at=stamp())
                # Keep the receipt; a repeated POST must never turn this into execution.
                return
            if command in JOBS:self.active_arm=name
            future=arm.send_goal(command,request_id=key,dispatch_guard=still_valid,envelope=envelope)
            future.add_done_callback(lambda done:self.complete(key,done))
        if command=='EMER_STOP':
            future=arm.send_goal(command,request_id=key)
            future.add_done_callback(lambda done:self.complete(key,done))
            self.save(r)
        else:
            # Reserve immediately while durable admission is pending; concurrent job
            # submissions cannot pass the global busy check during a slow fsync.
            if command in JOBS:arm.busy=True
            self.admissions.add(key)
            def admitted(saved):
                self.admissions.discard(key)
                # send_goal owns busy after admission. Do not clear another active job.
                if command in JOBS and (arm.journal.value.get('job') or {}).get('state') not in ('pending','unknown'):arm.busy=False
                dispatch(saved)
            if not self.save(r,on_saved=admitted):return reply(False,'STORAGE_ERROR','요청 기록 저장 실패 · 전송하지 않았습니다.',r)
        self.refresh_records()
        return reply(True,'ACCEPTED','접수 · 기록 저장과 실행부 결과 대기',r)

    def complete(self,key,future):
        r=self.records[key]
        try:
            result=future.result()
            state='SUCCEEDED' if result.success else 'CLEARED' if result.message=='CLEARED' else 'FAILED' if result.message in ('ERROR','NOT_FOUND') else 'UNKNOWN'
            detail,message=result.detail,result.message
        except Exception:r.update(state='UNKNOWN',detail='전송 결과 미확인',updated_at=stamp());self.save(r);return
        r.update(state=state,detail=detail,result_code=message,updated_at=stamp());self.save(r)

    def query(self,generation,key,command=None,arm_hint=None):
        self.valid_session(generation,require_ws=True)
        if not valid_id(key):raise BridgeError('INVALID_REQUEST','요청 ID 형식 오류',400)
        r=self.records.get(key)
        if r is None and command is not None:
            if command not in JOBS.keys()|CONTROLS or arm_hint not in self.arms or (command in JOBS and JOBS[command]!=arm_hint):
                raise BridgeError('INVALID_REQUEST','미확정 요청의 명령·대상 형식 오류',400)
            # A lost HTTP submission may have no API receipt. Only a Pi query with
            # the original ID can reconcile it; this path can never call send_goal.
            r=self.make_record(key,command,arm_hint,'UNKNOWN','접수 기록 없음 · 원래 ID로 실행부 조회')
            self.records[key]=r;self.save(r)
        if r is None:return None
        if command is not None and command!=r['command']:raise BridgeError('INVALID_REQUEST','요청 ID의 명령이 일치하지 않습니다.',409)
        # Until durable admission finishes, NOT_FOUND cannot prove that this
        # request will never execute. Querying now could publish failure first.
        if key in self.admissions:return deepcopy(r)
        if r['state'] not in TERMINAL:
            owner=self.arms[r['arm']]
            current=any((a.journal.value.get(slot) or {}).get('request_id')==key for a in self.arms.values() for slot in ('job','control'))
            candidates=[owner]
            if r['kind']=='CONTROL' and not current:
                candidates += [a for a in self.arms.values() if a is not owner]
            def query_next(index):
                arm=candidates[index]
                if not arm.health.online():return
                future=arm.query_request(r['command'],key,control=r['kind']=='CONTROL')
                def done(completed):
                    try:result=completed.result()
                    except Exception:
                        self.complete(key,completed);return
                    # A stop can precede both receipts. If its target hint was stale,
                    # an unaccepted query may try the other arm, still never execute.
                    if not getattr(result,'accepted',True) and index+1<len(candidates) and candidates[index+1].health.online():
                        query_next(index+1);return
                    if getattr(result,'accepted',True):r['arm']=arm.arm
                    self.complete(key,completed)
                future.add_done_callback(done)
            query_next(0)
        self.refresh_records();return deepcopy(r)

    def request_record(self,generation,key):
        self.valid_session(generation,require_ws=True)
        return deepcopy(self.records.get(key))

    def maintenance(self):
        self.expire();self.refresh_records()
        for key in list(self.dirty):
            if key in self.records and key not in self.saving:self.save(self.records[key])
