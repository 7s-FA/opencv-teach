"""GUI launch/observation of the same Pi episode runner used by work_watch."""
from copy import deepcopy
import json
import re
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time
import uuid

from .domain import atomic_json,JOINTS
from .arm_workspace import arm_id
from .episode_transfer import episode_snapshot
from .remote_config import configuration_bundle
from .remote_client import ssh_args


def active_execution(app):
    job=getattr(getattr(app,'workspace_manager',None),'pi_execution',None)
    return job if job and job.busy else None


def command_args(config,*,check=False,receiver='pi_execution_receiver.py',dispatch=False):
    script=Path(__file__).with_name(receiver).read_text()
    if receiver=='shutdown_receiver.py':
        # GUI adapters are normally streamed too, not installed on the Pi.
        adapter=Path(__file__).with_name('pi_execution_receiver.py').read_text()
        script=("import sys, types\n_adapter = types.ModuleType('so101_teach.pi_execution_receiver')\n"
                +"exec("+repr(adapter)+", _adapter.__dict__)\n"
                +"sys.modules[_adapter.__name__] = _adapter\n"+script)
    args=[config['python'],'-B','-c',script,config['app_dir']]
    if check:args.append('--check')
    if dispatch:args.append('--dispatch')
    command='if [ -f /opt/ros/jazzy/setup.bash ]; then . /opt/ros/jazzy/setup.bash; fi\n'
    command+='export ROS_DOMAIN_ID=40 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1\nexec '+' '.join(shlex.quote(s) for s in args)
    # Vision work may briefly delay this SSH stream. Motor supervision still
    # uses the independent two-second GUI heartbeat, not this transport timer.
    args=[{'ServerAliveInterval=2':'ServerAliveInterval=10','ServerAliveCountMax=2':'ServerAliveCountMax=3'}.get(x,x) for x in ssh_args(config)]
    return args+['-l',config['user'],config['host'],'bash -c '+shlex.quote(command)]


def execution_request(app,apps,*,apply_jig=True):
    from .episode_inspection import criteria_signature
    from .motion import SPEED_PRESETS
    episode=episode_snapshot(app)
    if app.episode_adjust_panel.dirty:raise ValueError('에피소드 조정 내용을 저장한 뒤 실행하세요.')
    links=execution_links(app,apps)
    bundle=configuration_bundle(app)
    bundle['mesh_hashes']={key:app.catalog.mesh(key)['sha256'] for key in app.catalog.items}
    return {'arm':arm_id(app.profile),'episode':episode,'bundle':bundle,'links':links,
            'speed':SPEED_PRESETS[app.motion_speed_choice.get()],
            'timeout_seconds':episode.get('pi_timeout_seconds',900.),'apply_jig':apply_jig,
            'inspection_protocol':6,'inspection_criteria_sha256':criteria_signature()}


def execution_links(app,apps):
    links={}
    config=app.require_remote().config
    for key in ('arm2','arm3'):
        other=apps.get(key);link=getattr(other,'remote',None);session=getattr(other,'session',None)
        if not other or not other.remote_mode or not link or link.error or link.stop.is_set() or not link.lease:
            raise ValueError('전체 연결을 완료한 뒤 에피소드를 실행하세요.')
        if any(link.config[k]!=config[k] for k in ('host','port','user','app_dir')):
            raise ValueError('두 팔의 Pi 연결 대상이 다릅니다.')
        sample=getattr(session,'latest',None)
        if (not session or not session.running or session.error or session.state not in ('READ_ONLY','HOLD')
                or session.command_pending.is_set() or session.program_active.is_set()
                or not sample or not sample.fresh() or not sample.calibration_matches
                or any(sample.telemetry.get(n,{}).get('moving')!=0 for n in JOINTS)):
            raise ValueError('두 팔의 최신 정지 상태를 확인한 뒤 실행하세요.')
        if (other.pending_execution or other.camera_task or other.safe_entry or other.remote_plan_job or other.playing
                or other.live_adjust.owner is not None or other.settings.job or other.settings.worker and other.settings.worker.running
                or getattr(getattr(other,'inspection_run',None),'busy',False)):
            raise ValueError('다른 측정·조정·실행을 마친 뒤 에피소드를 실행하세요.')
        links[key]={'port':link.server_port,'token':link.token,'lease':link.lease,'instance':link.instance}
    return links


STAGES={'LOWER':'하단','MIDDLE':'중단','UPPER':'상단','PICK':'집기','PLACE':'놓기'}
DONE_STATUSES=('A_DONE','B_DONE','GUI_DONE','A_CHAIN_DONE','B_CHAIN_DONE')
RESET_STATUSES=('A_RESET_DONE','B_RESET_DONE','GUI_RESET_DONE')

def execution_result(line,execution_id):
    """Only the receiver's per-execution result can close GUI ownership."""
    if not line.startswith('GUI_RESULT:'):return None
    try:value=json.loads(line[len('GUI_RESULT:'):])
    except (ValueError,TypeError):return None
    if (not isinstance(value,dict) or set(value)!={'execution_id','exit_code'}
            or value['execution_id']!=execution_id or type(value['exit_code']) is not int
            or value['exit_code'] not in (0,1,130)):return None
    return value['exit_code']
LABELS={'FOLLOWER_CONNECTING':'팔 연결 확인','FOLLOWER_READY':'팔 연결 확인 완료','ACCEPTED':'에피소드 접수',
        'WORKCELL_PREPARING':'양팔·카메라·모델 준비 중','WORKCELL_READY':'양팔·카메라·모델 준비 완료',
        'STANDBY_READY':'3분 준비 대기 · 다음 명령 수신 가능',
        'VISION_PREPARING':'카메라·모델 준비 중','VISION_READY':'카메라·모델 준비 완료',
        'JIG_DETECTION_STARTED':'지그 위치 확인 중','JIG_CONFIRMED':'지그 위치 확인 완료',
        'STARTUP_INSPECTION_STARTED':'시작 조건 검사 중','STARTUP_INSPECTION_PASSED':'시작 조건 검사 통과',
        'PLAN_STARTED':'동작 경로 준비','EPISODE_STARTED':'에피소드 실행 중','EPISODE_DONE':'동작 완료 · 마무리 중',
        'DONE':'에피소드 완료','PAUSED':'팔 정지 · 작업 보류','RESUMED':'작업 재개',
        'INSPECTION_PASS':'안착 검사 통과','INSPECTION_GROUP_STARTED':'안착 검사 중',
        'INSPECTION_PROVISIONAL_PASS':'안착 확인 · 최종 지그 위치 확인 대기',
        'INSPECTION_INVALIDATED':'지그 위치 변경 · 안착 재검사',
        'RECOVERY_REQUIRED':'판정 실패 · 재검사 후 재개 대기',
        'RECOVERY_RECHECK_STARTED':'재개 전 상태·영상 재검사 중',
        'RECOVERY_RECHECK_PASSED':'재검사 통과 · 이어서 실행',
        'RECOVERY_RECHECK_CANCELLED':'새 정지 요청 · 재검사 취소',
        'RECOVERY_BLOCKED':'재개 조건 미충족 · 보류 유지',
        'INSPECTION_RETRY_REQUIRED':'검사 재시도 필요',
        'INSPECTION_RECHECK_STARTED':'체크포인트 새 영상 검사 중',
        'INSPECTION_FAILED':'안착 검사 실패','FAILED':'에피소드 중단','REJECTED':'실행 거절'}


def status_text(line):
    prefix,_,detail=line.partition('_')
    if prefix not in ('A','B','GUI','SLIDE'):return line
    kind,_,value=detail.partition(':')
    if kind=='STAGE_DONE':return STAGES.get(value,value)+' 완료'
    if kind in ('LINEAR_CHECK','LINEAR_STARTED','LINEAR_READY','LINEAR_DONE'):
        from .linear_state import position_label
        target=re.search(r'target_mm=([0-9.]+)',value)
        label=position_label({},float(target[1])) if target else '명령 위치 미확인'
        stage={'LINEAR_CHECK':'확인 중','LINEAR_STARTED':'이동 중','LINEAR_READY':'구동 완료 확인','LINEAR_DONE':'구동 시간 완료'}[kind]
        return '리니어 : '+label+' · '+stage
    if kind=='STEP_DONE':return '스텝 완료 · '+value
    return LABELS.get(kind,kind)+(' · '+value if value else '')


class PiExecution:
    def __init__(self,app,*,apply_jig=True):
        self.app=app;self.manager=app.workspace_manager
        if not self.manager:raise ValueError('두 팔 작업창에서 실행하세요.')
        if active_execution(app):raise ValueError('에피소드가 실행 중입니다.')
        self.request=self.make_request(app,apply_jig=apply_jig)
        self.request['execution_id']=uuid.uuid4().hex
        self.apps=list(self.manager.apps.values());self.config=deepcopy(app.remote.config)
        self.links=[(key,other,other.remote,getattr(other,'session',None)) for key,other in self.manager.apps.items()]
        self.link=app.remote;self.state_lock=threading.Lock()
        self.paused=False;self.rechecking=False;self.restart_pending=False;self.pause_generation=None;self.recheck_required=False
        self.busy=True;self.process=None;self.thread=None;self.events=queue.Queue();self.cancelled=threading.Event();self.completion_received=threading.Event();self.write_lock=threading.Lock()
        self.reset_received=threading.Event();self.terminal_received=threading.Event()
        self.release_arms=set();self.last_saved=0.;self.started_at=time.monotonic();self.last_line='';self.last_visible='';self.launch_error=None
        self.record={'execution_id':self.request['execution_id'],'episode_id':self.request['episode']['id'],'robot_id':self.request['arm'],
                     'episode_name':self.request['episode']['name'],'state':'preparing','events':[]}
        self.path=app.data_dir/'diagnostics'/f'pi-episode-{time.time_ns()}.json'
        self.prepared=threading.Event()
        self.manager.pi_execution=self
        for other in self.apps:
            other.remote.execution_owner=self
            other.notice('Pi 에피소드 준비 · 리니어·검사·완료 알림 포함')
        try:
            # Release active camera writers before the Pi runner owns modes.
            for other in self.apps:other.suspend_camera()
            app.set_mode('live')
        except Exception as exc:
            self.finish(1,str(exc));raise
        self.prepare_thread=threading.Thread(target=self.prepare,daemon=True)
        self.prepare_thread.start()
        self.poll()

    def make_request(self,app,*,apply_jig):
        return execution_request(app,self.manager.apps,apply_jig=apply_jig)

    def receiver_command(self):
        return command_args(self.config)

    def prepare(self):
        # Slow fsync/claim RPCs must not starve Tk's authoritative heartbeat.
        try:
            atomic_json(self.path,self.record)
            for _,other,link,_ in self.links:
                if self.cancelled.is_set():break
                self.validate_connections()
                if other is self.app:link.rpc('claim_episode')
                else:link.rpc('claim_episode',{'preserve_idle_release':True})
        except Exception as exc:self.launch_error=str(exc)
        finally:self.prepared.set()

    def link_matches_request(self,key,link):
        expected=self.request.get('links',{}).get(key)
        return expected is None or all(getattr(link,attribute,None)==expected[name]
            for name,attribute in (('port','server_port'),('token','token'),('lease','lease'),('instance','instance')))

    def validate_connections(self):
        for key,other,link,_ in self.links:
            if other.remote is not link or not self.link_matches_request(key,link):
                raise RuntimeError('Pi 연결 대상이 변경되었습니다. 다시 연결한 뒤 실행하세요.')
            if link.error or link.stop.is_set():raise RuntimeError('Pi 연결 오류 · 실행 시작 취소')
        if self.link.config!=self.config:raise RuntimeError('Pi 연결 설정이 변경되었습니다. 다시 실행하세요.')

    def hold(self):
        # Never send an old job's stop through a replacement connection/lease.
        for key,other,link,_ in self.links:
            if other is self.app:
                if not self.link_matches_request(key,link):raise RuntimeError('실행 연결 식별자 변경 · 기존 연결 정지 확인 불가')
                return link.rpc('command',{'action':'hold'})

    def start(self):
        if not self.prepared.is_set():return
        if self.launch_error:self.finish(1,self.launch_error);return
        if self.cancelled.is_set():self.finish(1,'실행 시작 전 사용자 정지');return
        if any(a.camera and a.camera.running for a in self.apps):
            if time.monotonic()-self.started_at>8:self.finish(1,'카메라 소유권 전환 시간 초과')
            return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        try:
            with self.write_lock:
                if self.cancelled.is_set():self.events.put(('exit',1));return
                self.validate_connections()
                self.process=subprocess.Popen(self.receiver_command(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT,text=True,bufsize=1)
                self.process.stdin.write(json.dumps(self.request,ensure_ascii=False,allow_nan=False)+'\n');self.process.stdin.flush()
            for line in self.process.stdout:
                value=line.rstrip()[:2000]
                result=execution_result(value,self.request['execution_id'])
                if result is not None:
                    with self.state_lock:
                        if self.terminal_received.is_set():continue
                        self.terminal_received.set()
                        if result==0:self.completion_received.set()
                        elif result==130:self.reset_received.set()
                    if result==1:
                        # Retain ownership until this failure's final HOLD has
                        # returned. A delayed HOLD must not stop a subsequent job.
                        try:self.hold()
                        except Exception as exc:self.events.put(('line','GUI_FAILED:정지 응답 확인 실패: '+str(exc)))
                    self.events.put(('result',result));continue
                self.events.put(('line',value))
            code=self.process.wait()
            if not self.terminal_received.is_set():
                try:self.hold()
                except Exception as exc:self.events.put(('line','GUI_FAILED:정지 응답 확인 실패: '+str(exc)))
            self.events.put(('exit',code))
        except Exception as exc:
            if not self.terminal_received.is_set():
                try:self.hold()
                except Exception:pass
            # Closing stdin cancels the remote runner; keep ownership until SSH
            # has actually exited, even when writing/reading its stream failed.
            if self.process:
                try:self.process.stdin.close()
                except (OSError,ValueError):pass
                try:self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
            self.events.put(('error',str(exc)))

    @property
    def can_restart(self):
        return bool(self.busy and self.paused and not self.rechecking and not self.restart_pending
                    and type(self.pause_generation) is int and not self.cancelled.is_set()
                    and not self.terminal_received.is_set())

    def restart(self):
        with self.state_lock:
            if not self.can_restart:raise ValueError('재개할 보류 작업이 없거나 재검사 중입니다.')
            self.validate_connections()
            if not self.process or self.process.poll() is not None:raise ValueError('Pi 실행 연결이 종료되었습니다.')
            generation=self.pause_generation;self.restart_pending=True
            value={'job_id':self.request['execution_id'],'request_id':uuid.uuid4().hex,
                   'command':'restart','stop_generation':generation}
        def send():
            try:
                with self.write_lock:
                    self.validate_connections()
                    if self.cancelled.is_set() or self.terminal_received.is_set() or self.pause_generation!=generation:
                        raise RuntimeError('정지·연결 상태 변경으로 재개 요청을 취소했습니다.')
                    self.process.stdin.write('control '+json.dumps(value,separators=(',',':'))+'\n');self.process.stdin.flush()
            except Exception as exc:self.events.put(('line','CONTROL_REJECTED:'+str(exc)))
        self.restart_worker=threading.Thread(target=send,daemon=True,name='gui-episode-restart');self.restart_worker.start()
        self.app.notice('Pi 재개 요청 · 상태 확인과 재검사 완료 대기')

    def control_progress(self,line):
        context=line.partition('_RECOVERY_CONTEXT:')
        if context[1]:
            try:value=json.loads(context[2])
            except (TypeError,ValueError):return True
            if (not isinstance(value,dict) or value.get('job_id')!=self.request['execution_id']
                    or type(value.get('stop_generation')) is not int or value['stop_generation']<0):return True
            if self.pause_generation is not None and value['stop_generation']<self.pause_generation:return True
            if self.pause_generation!=value['stop_generation']:self.restart_pending=False
            self.pause_generation=value['stop_generation'];self.recheck_required=value.get('recheck_required') is True
            return True
        if line.endswith('_PAUSED'):self.paused=True
        elif line.endswith('_RESUMED'):
            self.paused=False;self.rechecking=False;self.restart_pending=False
        elif line.endswith('_RECOVERY_RECHECK_STARTED'):
            self.rechecking=True;self.restart_pending=True
        elif '_RECOVERY_BLOCKED:' in line or line.endswith('_RECOVERY_RECHECK_CANCELLED') or line.startswith('CONTROL_REJECTED:'):
            self.rechecking=False;self.restart_pending=False
        return False

    def cancel(self,action='hold',app=None):
        if not self.busy:return
        with self.state_lock:
            if action=='release':self.release_arms.add(arm_id((app or self.app).profile))
            if self.terminal_received.is_set():return
            self.cancelled.set()
        # Network/pipe writes stay off Tk; UI heartbeats must not pause here.
        def send():
            try:
                with self.write_lock:
                    if self.process and self.process.poll() is None:
                        self.process.stdin.write('cancel\n');self.process.stdin.flush()
            except (OSError,ValueError):pass
            # An immediate HOLD covers cancellation while vision/planning waits.
            try:self.hold()
            except Exception as exc:self.events.put(('line','GUI_FAILED:정지 응답 확인 실패: '+str(exc)))
        if not getattr(self,'cancel_sent',False):
            self.cancel_sent=True;self.cancel_worker=threading.Thread(target=send,daemon=True);self.cancel_worker.start()
            self.app.notice('에피소드 정지 요청 · Pi 실행 종료 확인 중')

    def poll(self):
        if not self.busy:return
        if not (self.terminal_received.is_set() or self.completion_received.is_set() or self.reset_received.is_set()) and any(link.error or link.stop.is_set() for _,_,link,_ in self.links):self.cancel()
        if self.thread is None:self.start()
        for _ in range(100):
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='line':
                self.last_line=value
                self.record['events'].append({'at':time.time(),'status':value})
                if self.control_progress(value):continue
                # Keep stage/inspection results visible instead of replacing them
                # with routine motor/vision sampling details in the same tick.
                if not any('_'+key in value for key in ('INSPECTION_COUNT:','LINEAR_PROGRESS:','JIG_POSE:','JIG_DETECTED:','FOLLOWER_','TORQUE_OFF_')):
                    self.last_visible='Pi 실행 종료 확인 중' if value in DONE_STATUSES+RESET_STATUSES else status_text(value)
                    self.app.notice(self.last_visible,'FAILED' in value or 'REJECTED' in value)
            elif kind=='result':
                self.terminal_received.set()
                if value==0:self.completion_received.set()
                elif value==130:self.reset_received.set()
                self.record['events'].append({'at':time.time(),'status':'GUI_RESULT:'+str(value)})
                self.finish(value);return
            elif kind=='exit':self.finish(value);return
            else:self.finish(1,value);return
        if self.busy:
            if self.record['events'] and time.monotonic()-self.last_saved>=1:
                self.record['state']='paused' if self.paused else 'running';self.last_saved=time.monotonic()
                try:atomic_json(self.path,self.record)
                except OSError:pass
            self.app.root.after(80,self.poll)

    def finish(self,code,detail=''):
        if not self.busy:return
        if getattr(self,'cancel_worker',None) and self.cancel_worker.is_alive():
            self.app.root.after(50,lambda:self.finish(code,detail));return
        success=code==0 and self.completion_received.is_set() and not self.cancelled.is_set()
        stopped=self.cancelled.is_set() or code in (0,130) and self.reset_received.is_set()
        self.record.update(state='done' if success else 'stopped' if stopped else 'failed',
                           finished_at=time.time(),error=detail or ('' if success else self.last_line))
        try:atomic_json(self.path,self.record)
        except OSError as exc:detail+=' · 실행 기록 저장 실패: '+str(exc)
        self.busy=False
        for _,other,link,_ in self.links:
            if other.remote is link and getattr(other.camera,'observer',False):other.suspend_camera()
            if getattr(link,'execution_owner',None) is self:link.execution_owner=None
            # The Pi runner may have configured the snapshot. Resync only on the
            # next explicit GUI operation, never overwrite it during a job.
            link.bundle_hash=None
        for key in self.release_arms:
            try:
                _,other,link,session=next(item for item in self.links if item[0]==key)
                if other.remote is not link or getattr(other,'session',None) is not session or not self.link_matches_request(key,link):
                    raise RuntimeError('연결이 변경되어 토크 해제 요청을 전달하지 않았습니다.')
                session.request('release')
            except Exception as exc:detail+=' · 토크 해제 확인 실패: '+str(exc)
        message='에피소드 완료 · Pi 실행 및 알림 기록 완료' if success else '에피소드 정지' if stopped else '에피소드 실행 실패'
        self.app.notice(message+(' · '+detail if detail else ' · '+self.last_visible if not success else ''),not success and not stopped)
