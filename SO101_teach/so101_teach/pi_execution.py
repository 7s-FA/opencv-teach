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
    bundle=configuration_bundle(app)
    bundle['mesh_hashes']={key:app.catalog.mesh(key)['sha256'] for key in app.catalog.items}
    return {'arm':arm_id(app.profile),'episode':episode,'bundle':bundle,'links':links,
            'speed':SPEED_PRESETS[app.motion_speed_choice.get()],
            'timeout_seconds':episode.get('pi_timeout_seconds',900.),'apply_jig':apply_jig,
            'inspection_protocol':6,'inspection_criteria_sha256':criteria_signature()}


STAGES={'LOWER':'하단','MIDDLE':'중단','UPPER':'상단','PICK':'집기','PLACE':'놓기'}
LABELS={'FOLLOWER_CONNECTING':'팔 연결 확인','FOLLOWER_READY':'팔 연결 확인 완료','ACCEPTED':'에피소드 접수',
        'JIG_DETECTION_STARTED':'지그 위치 확인 중','JIG_CONFIRMED':'지그 위치 확인 완료',
        'STARTUP_INSPECTION_STARTED':'시작 조건 검사 중','STARTUP_INSPECTION_PASSED':'시작 조건 검사 통과',
        'PLAN_STARTED':'동작 경로 준비','EPISODE_STARTED':'에피소드 실행 중','EPISODE_DONE':'동작 완료 · 마무리 중',
        'DONE':'에피소드 완료','PAUSED':'팔 정지 · 작업 보류','RESUMED':'작업 재개',
        'INSPECTION_PASS':'안착 검사 통과','INSPECTION_GROUP_STARTED':'안착 검사 중',
        'INSPECTION_FAILED':'안착 검사 실패','FAILED':'에피소드 중단','REJECTED':'실행 거절'}


def status_text(line):
    prefix,_,detail=line.partition('_')
    if prefix not in ('A','B','GUI','SLIDE'):return line
    kind,_,value=detail.partition(':')
    if kind=='STAGE_DONE':return STAGES.get(value,value)+' 완료'
    if kind in ('LINEAR_CHECK','LINEAR_STARTED','LINEAR_READY','LINEAR_DONE'):
        from .linear_state import direction_label
        target=re.search(r'target_mm=([0-9.]+)',value)
        label=direction_label({},float(target[1])) if target else '목표 위치'
        stage={'LINEAR_CHECK':'확인 중','LINEAR_STARTED':'이동 중','LINEAR_READY':'구동 완료 확인','LINEAR_DONE':'구동 시간 완료'}[kind]
        return '리니어 '+label+' · '+stage+(' · 위치 미측정' if kind in ('LINEAR_READY','LINEAR_DONE') else '')
    if kind=='STEP_DONE':return '스텝 완료 · '+value
    return LABELS.get(kind,kind)+(' · '+value if value else '')


class PiExecution:
    def __init__(self,app,*,apply_jig=True):
        self.app=app;self.manager=app.workspace_manager
        if not self.manager:raise ValueError('두 팔 작업창에서 실행하세요.')
        if active_execution(app):raise ValueError('에피소드가 실행 중입니다.')
        self.request=execution_request(app,self.manager.apps,apply_jig=apply_jig)
        self.apps=list(self.manager.apps.values());self.config=deepcopy(app.remote.config)
        self.busy=True;self.process=None;self.thread=None;self.events=queue.Queue();self.cancelled=threading.Event();self.completion_received=threading.Event();self.write_lock=threading.Lock()
        self.release_arms=set();self.last_saved=0.;self.started_at=time.monotonic();self.last_line='';self.last_visible='';self.launch_error=None
        self.record={'episode_id':self.request['episode']['id'],'robot_id':self.request['arm'],
                     'episode_name':self.request['episode']['name'],'state':'preparing','events':[]}
        self.path=app.data_dir/'diagnostics'/f'pi-episode-{time.time_ns()}.json'
        atomic_json(self.path,self.record)
        # Claim the next job before camera/SSH preparation, cancelling the old
        # idle timer as soon as the command reaches each controller.
        for other in self.apps:other.remote.rpc('claim_episode')
        # Release active camera writers before the Pi runner owns camera modes.
        for other in self.apps:other.suspend_camera()
        self.manager.pi_execution=self
        for other in self.apps:
            other.remote.execution_owner=self
            other.notice('Pi 에피소드 준비 · 리니어·검사·완료 알림 포함')
        app.set_mode('live')
        self.poll()

    def start(self):
        if self.cancelled.is_set():self.finish(1,'실행 시작 전 사용자 정지');return
        if any(a.camera and a.camera.running for a in self.apps):
            if time.monotonic()-self.started_at>8:self.finish(1,'카메라 소유권 전환 시간 초과')
            return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()

    def run(self):
        try:
            with self.write_lock:
                if self.cancelled.is_set():self.events.put(('exit',1));return
                self.process=subprocess.Popen(command_args(self.config),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT,text=True,bufsize=1)
                self.process.stdin.write(json.dumps(self.request,ensure_ascii=False,allow_nan=False)+'\n');self.process.stdin.flush()
            for line in self.process.stdout:
                value=line.rstrip()[:2000]
                if value in ('A_DONE','B_DONE','GUI_DONE','A_CHAIN_DONE','B_CHAIN_DONE'):self.completion_received.set()
                self.events.put(('line',value))
            code=self.process.wait()
            if code and not self.completion_received.is_set():
                try:self.app.remote.rpc('command',{'action':'hold'})
                except Exception as exc:self.events.put(('line','GUI_FAILED:정지 응답 확인 실패: '+str(exc)))
            self.events.put(('exit',code))
        except Exception as exc:
            if not self.completion_received.is_set():
                try:self.app.remote.rpc('command',{'action':'hold'})
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

    def cancel(self,action='hold',app=None):
        if not self.busy:return
        if action=='release':self.release_arms.add(arm_id((app or self.app).profile))
        self.cancelled.set()
        # Network/pipe writes stay off Tk; UI heartbeats must not pause here.
        def send():
            try:
                with self.write_lock:
                    if self.process and self.process.poll() is None:
                        self.process.stdin.write('cancel\n');self.process.stdin.flush()
            except (OSError,ValueError):pass
            # An immediate HOLD covers cancellation while vision/planning waits.
            link=self.app.remote
            try:link.rpc('command',{'action':'hold'})
            except Exception as exc:self.events.put(('line','GUI_FAILED:정지 응답 확인 실패: '+str(exc)))
        if not getattr(self,'cancel_sent',False):
            self.cancel_sent=True;self.cancel_worker=threading.Thread(target=send,daemon=True);self.cancel_worker.start()
            self.app.notice('에피소드 정지 요청 · Pi 실행 종료 확인 중')

    def poll(self):
        if not self.busy:return
        if self.thread is None:self.start()
        for _ in range(100):
            try:kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='line':
                self.last_line=value
                self.record['events'].append({'at':time.time(),'status':value})
                # Keep stage/inspection results visible instead of replacing them
                # with routine motor/vision sampling details in the same tick.
                if not any('_'+key in value for key in ('INSPECTION_COUNT:','LINEAR_PROGRESS:','JIG_POSE:','JIG_DETECTED:','FOLLOWER_','TORQUE_OFF_')):
                    self.last_visible=status_text(value)
                    self.app.notice(self.last_visible,'FAILED' in value or 'REJECTED' in value)
                if value in ('A_DONE','B_DONE','GUI_DONE','A_CHAIN_DONE','B_CHAIN_DONE'):
                    self.finish(0);return
            elif kind=='exit':self.finish(value);return
            else:self.finish(1,value);return
        if self.busy:
            if self.record['events'] and time.monotonic()-self.last_saved>=1:
                self.record['state']='running';self.last_saved=time.monotonic()
                try:atomic_json(self.path,self.record)
                except OSError:pass
            if any(a.remote.error or a.remote.stop.is_set() for a in self.apps):self.cancel()
            self.app.root.after(80,self.poll)

    def finish(self,code,detail=''):
        if not self.busy:return
        if getattr(self,'cancel_worker',None) and self.cancel_worker.is_alive():
            self.app.root.after(50,lambda:self.finish(code,detail));return
        success=code==0 and self.last_line.endswith('_DONE') and not self.cancelled.is_set()
        self.record.update(state='done' if success else 'stopped' if self.cancelled.is_set() else 'failed',
                           finished_at=time.time(),error=detail or ('' if success else self.last_line))
        try:atomic_json(self.path,self.record)
        except OSError as exc:detail+=' · 실행 기록 저장 실패: '+str(exc)
        self.busy=False
        for other in self.apps:
            if getattr(other.camera,'observer',False):other.suspend_camera()
            if getattr(other.remote,'execution_owner',None) is self:other.remote.execution_owner=None
            # The Pi runner may have configured the snapshot. Resync only on the
            # next explicit GUI operation, never overwrite it during a job.
            other.remote.bundle_hash=None
        for key in self.release_arms:
            try:self.manager.apps[key].session.request('release')
            except Exception as exc:detail+=' · 토크 해제 확인 실패: '+str(exc)
        message='에피소드 완료 · Pi 실행 및 알림 기록 완료' if success else '에피소드 정지' if self.cancelled.is_set() else '에피소드 실행 실패'
        self.app.notice(message+(' · '+detail if detail else ' · '+self.last_visible if not success else ''),not success and not self.cancelled.is_set())
