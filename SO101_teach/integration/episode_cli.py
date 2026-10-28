"""Pi-local episode command; uses the existing device runtime without a PC UI."""
import argparse
from datetime import datetime
from dataclasses import dataclass
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, TimeoutError as PlanTimeout
import fcntl
import json
import os
from pathlib import Path
import signal
import select
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from so101_teach.domain import read_json, atomic_json, load_profile, EpisodeStore
from so101_teach.remote_config import configuration_bundle
from so101_teach.preparation_profile import PreparationProfile,timed
from so101_teach.episode_faults import RetryableFault,InspectionRetry,JigRetry,ResumeBlocked,RecheckInterrupted


INSPECTION_PROTOCOL = 6


class ResetRequested(Exception):
    pass


class ShutdownRequested(Exception):
    pass


class PreparationRestart(Exception):
    pass


def chain_start_records(jobs):
    """Validate both recipe start poses before opening either controller."""
    records={}
    for arm,job in jobs.items():
        _,episode,bundle,_,_=job
        first=episode['steps'][0]
        if (first.get('safe_boundary')!='start' or first.get('jig_id')
                or not isinstance(first.get('ticks'),dict) or len(first['ticks'])!=6):
            raise ValueError(arm+': 연속 실행에는 저장된 시작 안전자세가 필요합니다.')
        records[arm]={'safe_ticks':deepcopy(first['ticks']),'bundle':deepcopy(bundle)}
    return records


def finished_return_step(episode):
    """Use this run's final full-product seating check, never a saved result."""
    checks = [step['id'] for step in episode['steps']
              if step.get('inspection', {}).get('station') == 'finished'
              and step['inspection'].get('target') == 'pallet'
              and step['inspection'].get('expected') == 'cap_added']
    if not checks:
        raise ValueError('BUILD LOAD 연속 실행: 운송 에피소드에 완성품 팔레트의 완제품 안착 검사가 필요합니다.')
    return checks[-1]


def load_job(arm, command, *, timing=None):
    began=time.monotonic()
    from so101_teach.configuration import JigCatalog
    from so101_teach.vision import PoseLatch
    from so101_teach.arm_workspace import require_calibrated
    if timing:timing.add(arm+'.load.imports',time.monotonic()-began)
    data = ROOT / ('data' if arm == 'arm2' else 'data/arm3-runtime')
    profile, cal, reference = timed(timing,arm+'.load.profile',load_profile,data)
    if profile.get('robot_id') != arm:
        raise ValueError('다른 팔의 설정입니다.')
    require_calibrated(profile)
    settings = read_json(ROOT / 'integration/execution-settings.json')[arm]
    reference.set_trims(settings['trim_ticks'])
    from so101_teach.execution_resources import execution_catalog
    catalog = timed(timing,arm+'.load.catalog',execution_catalog,data,profile)
    latch = PoseLatch(settings['hold_seconds'], settings['acquisition_seconds'], settings['acquisition_attempts'])
    app = SimpleNamespace(profile=profile, data_dir=data, catalog=catalog, reference=reference,
                          pose_latch=latch, active_jig=next(iter(catalog.items)))
    recipe = read_json(ROOT / 'integration/recipes.json')['recipes'][arm][command]
    episode_id = recipe['episode_id']
    if len(episode_id) != 32 or any(c not in '0123456789abcdef' for c in episode_id):
        raise ValueError('에피소드 ID 오류')
    episode = timed(timing,arm+'.load.episode',EpisodeStore(data / 'episodes', cal, arm).load,data / 'episodes' / (episode_id + '.json'))
    if not episode['steps']:
        raise ValueError('저장된 스텝이 없습니다.')
    events=episode.get('completion_events')
    if events is not None:
        steps={s['id']:s for s in episode['steps']}
        if (not isinstance(events,dict) or set(events)-{'LOWER','MIDDLE','UPPER','PICK','PLACE'}
                or any(not isinstance(key,str) or key not in steps or steps[key].get('safe_boundary') for key in events.values())
                or len(set(events.values()))!=len(events)):
            raise ValueError('에피소드 완료 알림 지정 오류')
    from so101_teach.episode_inspection import validate_inspection,has_inspections
    validate_inspection(episode)
    from so101_teach.episode_inspection import inspection_schedule
    inspection_schedule(episode['steps'])
    if has_inspections(episode):
        from so101_teach.episode_inspection_runtime import require_resources
        require_resources()
    from so101_teach.model_regions import verify_profile_models
    timed(timing,arm+'.load.model_verify',verify_profile_models,profile,inspect_parts=has_inspections(episode))
    bundle = timed(timing,arm+'.load.bundle',configuration_bundle,app)
    bundle['mesh_hashes'] = timed(timing,arm+'.load.meshes',lambda:{key:catalog.mesh(key)['sha256'] for key in catalog.items})
    return data, episode, bundle, settings['speed'], float(recipe['timeout_seconds'])


def open_links(*links):
    """Only independent I/O runs here; controls remain on the coordinator."""
    with ThreadPoolExecutor(max_workers=len(links),thread_name_prefix='episode-connect') as pool:
        futures=[pool.submit(link.open) for link in links]
        # Drain all attempts before the caller releases any acquired leases.
        for future in futures:future.result()


class Link:
    def __init__(self, arm):
        self.arm = arm
        self.lease = None
        self.stop = threading.Event()
        self.error = None
        self.initial_inactive_follower = None
        self.follower_required = False
        self.preserve_idle_release = False
        self.motion_observation = threading.local()
        self.timing=None

    def post(self, endpoint, body=None, timeout=5):
        request = Request(self.base + endpoint, data=json.dumps(body or {}).encode(),
                          headers={'Authorization': 'Bearer ' + self.token})
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        if not result['ok']:
            raise RuntimeError(result['error'])
        return result['value']

    def open(self):
        # Captured credentials never appear in terminal output or run records.
        result = timed(self.timing,self.arm+'.link.ensure',subprocess.run,[sys.executable, '-B', '-m', 'so101_teach.remote_server',
                                 '--ensure', '--arm', self.arm], cwd=ROOT, capture_output=True,
                                text=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-700:])
        info = json.loads(result.stdout)
        self.base = f"http://127.0.0.1:{info['port']}"
        self.token = info['token']
        self.lease = timed(self.timing,self.arm+'.link.acquire',self.post,'/acquire',
                          {'preserve_idle_release':True} if self.preserve_idle_release else {})['lease']
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()
        initial=timed(self.timing,self.arm+'.link.first_state',self.state)
        if normally_closed(initial):self.initial_inactive_follower=deepcopy(initial)

    def heartbeat(self):
        while not self.stop.is_set():
            try:
                state = self.post('/state', {'lease': self.lease, 'alive': True,'heartbeat_only':True}, timeout=1)
                if state['detached']:
                    raise RuntimeError('실행부 연결이 끊겼습니다.')
            except Exception as exc:
                self.error = str(exc)
                return
            self.stop.wait(.2)

    def rpc(self, method, args=None):
        if method=='connect' or method=='command':
            self.initial_inactive_follower=None;self.follower_required=True
        if self.error:
            raise RuntimeError(self.error)
        args=dict(args or {})
        if method=='configure' and getattr(self,'compute',None) is not None:args['compute']=self.compute.descriptor
        if method=='command' and args.get('action') not in ('hold','release'):
            generation=getattr(self.motion_observation,'stop_generation',None)
            if generation is not None:args['stop_generation']=generation
        return self.post('/rpc', {'lease': self.lease, 'id': uuid.uuid4().hex,
                                 'method': method, 'args': args}, timeout=40)

    def observe_motion_state(self,state):
        # Emergency checks run on a different thread. They must not refresh the
        # executing thread's pre-stop permission while its request is delayed.
        self.motion_observation.stop_generation=(state or {}).get('stop_generation')
        if state is None and self.follower_required:raise RuntimeError('이 작업에서 사용한 팔로워 상태를 확인할 수 없습니다.')
        if state and state.get('running'):
            self.initial_inactive_follower=None;self.follower_required=True
        return state

    def state(self):
        if self.error:
            raise RuntimeError(self.error)
        return self.observe_motion_state(self.rpc('execution_state'))

    def close(self):
        if self.lease:
            try:
                self.post('/detach', {'lease': self.lease,'retain_prepared':bool(getattr(self,'keep_prepared',False))})
            finally:
                self.stop.set()
                self.thread.join(2)


def normally_closed(state):
    """Recognize a previously verified OFF session without reviving its telemetry."""
    telemetry=((state or {}).get('latest') or {}).get('telemetry',{})
    return bool(state and not state.get('running') and not state.get('error')
                and state.get('state')=='READ_ONLY' and not state.get('program_active')
                and not state.get('command_pending') and not state.get('release_pending')
                and len(telemetry)==6 and all(h.get('torque')==0 and h.get('moving')==0 for h in telemetry.values()))


def settled(state,*,now=None):
    sample = (state or {}).get('latest') or {}
    telemetry = sample.get('telemetry', {})
    return bool(state and state['running'] and state['state'] == 'HOLD'
                and not state['program_active'] and not state['command_pending']
                and sample.get('calibration_matches') and 0 <= (time.monotonic() if now is None else now) - sample.get('monotonic', 0) < .5
                and len(telemetry) == 6 and all(h.get('moving') == 0 for h in telemetry.values()))


def torque_off(state,*,now=None):
    sample = (state or {}).get('latest') or {}
    telemetry = sample.get('telemetry', {})
    return bool(state and state['running'] and not state.get('error') and state['state'] == 'READ_ONLY'
                and not state['program_active'] and not state['command_pending']
                and 0 <= (time.monotonic() if now is None else now) - sample.get('monotonic', 0) < .5
                and len(telemetry) == 6 and all(h.get('torque') == 0 for h in telemetry.values()))


def stopped(state,*,now=None):
    """A torque-OFF confirmation alone does not prove the arm has stopped."""
    now=time.monotonic() if now is None else now
    telemetry=((state or {}).get('latest') or {}).get('telemetry',{})
    return bool(state and not state.get('error') and not state.get('release_pending',False)
                and (settled(state,now=now) or torque_off(state,now=now) or activation_paused(state,now=now))
                and len(telemetry)==6 and all(h.get('moving')==0 for h in telemetry.values()))


def activation_paused(state,*,now=None):
    from so101_teach.domain import JOINTS
    from so101_teach.motion import INTERNAL_TARGET_MARGIN,RUNAWAY_TICKS
    sample=(state or {}).get('latest') or {};evidence=(state or {}).get('activation_hold') or {}
    goals=evidence.get('goals');since=evidence.get('since');at=sample.get('monotonic')
    telemetry=sample.get('telemetry') or {};ticks=sample.get('ticks') or {}
    if (not state or state.get('state')!='ACTIVATION_PAUSED' or not state.get('running') or state.get('error')
            or state.get('release_pending') or state.get('program_active') or state.get('command_pending')
            or not sample.get('calibration_matches') or type(since) not in (int,float) or type(at) not in (int,float)
            or not since<=at or not 0<=(time.monotonic() if now is None else now)-at<.5
            or not isinstance(goals,dict) or not set(goals)<=set(JOINTS)
            or set(telemetry)!=set(JOINTS) or set(ticks)!=set(JOINTS)):return False
    for name in JOINTS:
        h=telemetry[name]
        if h.get('moving')!=0 or h.get('status') not in (0,1) or h.get('torque')!=(1 if name in goals else 0):return False
        if name in goals:
            if (type(goals[name]) is not int or type(ticks[name]) is not int or type(h.get('internal_goal_ticks')) is not int
                    or h.get('goal_ticks')!=goals[name] or abs(ticks[name]-goals[name])>RUNAWAY_TICKS
                    or abs(h['internal_goal_ticks']-goals[name])>INTERNAL_TARGET_MARGIN):return False
    return True


def at_safe_pose(state,target,*,now=None):
    """Reuse normal arrival limits only with complete, fresh torque-ON evidence."""
    from so101_teach.domain import JOINTS
    from so101_teach.motion import ARRIVAL_TOLERANCE_TICKS,BODY_ARRIVAL_TOLERANCE_TICKS
    sample=(state or {}).get('latest') or {};ticks=sample.get('ticks') or {};telemetry=sample.get('telemetry') or {}
    if (not state or state.get('error') or state.get('release_pending') or not settled(state,now=now)
            or set(target)!=set(JOINTS) or set(ticks)!=set(JOINTS) or set(telemetry)!=set(JOINTS)):
        return False
    return all(type(target[n]) is int and type(ticks[n]) is int
               and 0<=target[n]<=4095 and 0<=ticks[n]<=4095 and telemetry[n].get('torque')==1
               and abs(ticks[n]-target[n])<=(ARRIVAL_TOLERANCE_TICKS if n=='gripper' else BODY_ARRIVAL_TOLERANCE_TICKS)
               for n in JOINTS)


class Control:
    def __init__(self):
        self.pause = threading.Event()
        self.resume = threading.Event()
        self.reset = threading.Event()
        self.shutdown = threading.Event()
        self.paused = False
        self.rechecking = False;self.recovery=None
        self.ending = False
        self.ending_connections = ()
        self.ending_worker = None
        self.ending_confirmation_closed = False
        self.emergency = None
        self.info = None
        self.lock = threading.RLock()
        self.job_id=uuid.uuid4().hex;self.receipts={};self.stop_generation=0;self.estop_after=0.
        self.supervision_fd=None;self.supervision_lost=False
        self.startup_timing=PreparationProfile()

    def consume_supervision_stop(self):
        """Called under Control.lock, including just before enabling/consuming motion."""
        if self.supervision_fd is None:return
        stopped=False
        while True:
            try:data=os.read(self.supervision_fd,4096)
            except BlockingIOError:break
            if not data:
                self.supervision_lost=True;stopped=True;break
            stopped=True
        if stopped:self.command('estop')

    def configure_supervision(self,job_id,stop_fd,done):
        if job_id is not None:
            if len(job_id)!=32 or any(c not in '0123456789abcdef' for c in job_id):raise ValueError('invalid supervision job id')
            self.job_id=job_id
        if stop_fd is None:return None
        self.supervision_fd=stop_fd;os.set_blocking(stop_fd,False)
        def listen():
            try:
                while not done.is_set() and not self.supervision_lost:
                    if not select.select([stop_fd],[],[],.1)[0]:continue
                    try:
                        with self.lock:self.consume_supervision_stop()
                    except Exception as exc:
                        if self.shutdown.is_set():break
                        try:print('비상정지 신호 처리 실패: '+str(exc),file=sys.stderr)
                        except OSError:pass
            finally:
                with self.lock:self.supervision_fd=None
                os.close(stop_fd)
        # No pipe read occurs outside Control.lock, so a delayed receipt write
        # cannot hide an already queued stop from the motion permission check.
        with self.lock:self.consume_supervision_stop()
        thread=threading.Thread(target=listen,name='supervision-stop',daemon=True);thread.start()
        return thread

    def save_receipts(self):
        if self.receipts:
            atomic_json(ROOT/'data/episode-control-receipts'/f'{self.job_id}.json',
                        {'job_id':self.job_id,'receipts':deepcopy(self.receipts)})

    def complete(self,command):
        with self.lock:
            changed=[]
            for receipt in self.receipts.values():
                if receipt['command']==command and receipt['state']=='pending':
                    receipt['state']='completed';changed.append(receipt)
            if changed:
                try:self.save_receipts()
                except OSError:
                    for receipt in changed:receipt['state']='pending'
                    raise

    def confirm_estop(self,states):
        # Called after I/O; the same lock serializes newer stop requests.
        with self.lock:
            now=time.monotonic()
            if not all(stopped(s,now=now) and (s.get('latest') or {}).get('monotonic',0)>=self.estop_after for s in states):return False
            # A caller may request RESTART as soon as it sees completion.
            # Publish the paused state in the same critical section.
            if not self.shutdown.is_set():self.paused=True
            self.complete('estop')
            return True

    def fail_pending(self,*,exclude=()):
        with self.lock:
            changed=False
            for receipt in self.receipts.values():
                if receipt['state']=='pending' and receipt['command'] not in exclude:receipt['state']='failed';changed=True
            if changed:self.save_receipts()

    def begin_ending(self,connections):
        """Keep accepted stops observable throughout failed-job cleanup."""
        with self.lock:
            self.ending=True
            self.resume.clear();self.reset.clear()
            self.ending_connections=tuple(connections)
            self.start_ending_confirmation()

    def start_ending_confirmation(self):
        # Called under lock. One worker owns confirmation; no idle thread remains.
        # shutdown can precede camera cleanup; only finish_controls closes this owner.
        if not self.ending or self.ending_confirmation_closed or self.ending_worker is not None:return
        if not any(r['command']=='estop' and r['state']=='pending' for r in self.receipts.values()):return
        def confirm():
            try:
                while True:
                    with self.lock:
                        if not any(r['command']=='estop' and r['state']=='pending' for r in self.receipts.values()):return
                        connections=self.ending_connections
                    try:confirm_connected_stop(self,connections)
                    except Exception as exc:
                        try:self.fail_pending()
                        except Exception as error:
                            try:print('제어 완료 기록 정리 실패: '+str(error),file=sys.stderr)
                            except OSError:pass
                        try:print('종료 정리 중 정지 확인 실패: '+str(exc),file=sys.stderr)
                        except OSError:pass
            finally:
                with self.lock:
                    # An arriving request either sees this owner or starts the next;
                    # finalization seals admission before joining the current owner.
                    self.ending_worker=None
                    self.start_ending_confirmation()
        self.ending_worker=threading.Thread(target=confirm,name='episode-ending-stop',daemon=True)
        self.ending_worker.start()

    def request(self,value):
        if value.get('job_id')!=self.job_id:raise ValueError('JOB_CHANGED')
        key=value.get('request_id','');command=value.get('command')
        if len(key)!=32 or any(c not in '0123456789abcdef' for c in key) or command not in ('estop','restart','reset'):
            raise ValueError('INVALID_CONTROL_REQUEST')
        previous=self.receipts.get(key)
        if previous:
            if previous['command']!=command:raise ValueError('REQUEST_CHANGED')
            if command=='estop':self.start_ending_confirmation()
            return json.dumps(previous)
        motion=command in ('restart','reset')
        if motion:
            self.consume_supervision_stop()
            if self.supervision_lost:raise ValueError('SUPERVISION_LOST')
            if type(value.get('stop_generation')) is not int or value['stop_generation']!=self.stop_generation:raise ValueError('STALE_CONTROL_AFTER_STOP')
            if self.shutdown.is_set() or self.ending:raise ValueError('JOB_ENDING')
            if not self.paused:raise ValueError('NOT_PAUSED')
            if command=='restart' and self.rechecking:raise ValueError('CONTROL_PENDING')
            if self.resume.is_set() or self.reset.is_set() or any(r['command']=='estop' and r['state']=='pending' for r in self.receipts.values()):raise ValueError('CONTROL_PENDING')
        else:self.command(command)
        receipt={'job_id':self.job_id,'request_id':key,'command':command,
                 'state':'pending'}
        if motion:receipt['stop_generation']=value['stop_generation']
        self.receipts[key]=receipt
        try:self.save_receipts()
        except OSError:
            if motion:receipt['state']='failed'
            raise
        if motion:
            # Persist before setting a motion flag; drain stops queued during fsync.
            self.consume_supervision_stop()
            if self.supervision_lost or value['stop_generation']!=self.stop_generation:
                receipt['state']='cancelled';raise ValueError('STALE_CONTROL_AFTER_STOP')
            self.command(command)
        else:self.start_ending_confirmation()
        return json.dumps(receipt)

    def command(self, command):
        with self.lock:
            if command == 'inspect':
                return json.dumps({'info': self.info, 'paused': self.paused,'job_id':self.job_id,'stop_generation':self.stop_generation,
                                   'receipts':dict(list(self.receipts.items())[-16:]),'recovery':self.recovery})
            if command.startswith('control '):return self.request(json.loads(command[8:]))
            if self.shutdown.is_set(): raise ValueError('JOB_ENDING')
            if command.startswith('clear_error '):
                if not self.info or command != 'clear_error ' + self.info['run_id']:
                    raise ValueError('JOB_CHANGED')
                if not self.paused or self.resume.is_set() or self.reset.is_set():
                    raise ValueError('NOT_PAUSED_OR_CONTROL_PENDING')
                self.shutdown.set()
                return 'CLEAR_ERROR_REQUESTED'
            if command == 'estop':
                self.stop_generation+=1
                changed=False
                for receipt in self.receipts.values():
                    if receipt['command'] in ('restart','reset') and receipt['state']=='pending':
                        receipt['state']='cancelled';changed=True
                self.resume.clear(); self.reset.clear(); self.pause.set()
                try:
                    errors=self.emergency() if self.emergency else None
                finally:
                    self.estop_after=time.monotonic()
                    if changed:self.save_receipts()
                if isinstance(errors,(list,tuple)) and errors:raise RuntimeError('정지 요청 실패: '+'; '.join(errors))
                return 'ESTOP_REQUESTED'
            if command in ('restart', 'reset'):
                if self.ending:raise ValueError('JOB_ENDING')
                if not self.paused: raise ValueError('NOT_PAUSED')
                if command=='restart' and self.rechecking:raise ValueError('CONTROL_PENDING')
                if self.resume.is_set() or self.reset.is_set() or any(r['command']=='estop' and r['state']=='pending' for r in self.receipts.values()): raise ValueError('CONTROL_PENDING')
                (self.resume if command == 'restart' else self.reset).set()
                return command.upper() + '_REQUESTED'
            raise ValueError('UNKNOWN_CONTROL')


def confirm_connected_stop(control,connections,*,timeout=3.):
    """Confirm current stop evidence without closing the control endpoint."""
    began=time.monotonic();active=[]
    # Both reserved arms are checked; a failed state query is never success.
    for link in connections:
        if link is None or not link.lease:continue
        state=link.state()
        if state is None:continue  # No follower has been connected on this lease.
        if not state.get('running'):
            if normally_closed(state) and state==getattr(link,'initial_inactive_follower',None):continue
            raise RuntimeError('정지 확인 중 팔로워 연결 종료')
        if not torque_off(state):
            link.post('/rpc',{'lease':link.lease,'id':uuid.uuid4().hex,
                'method':'command','args':{'action':'hold'}})
        active.append((link,time.monotonic()))
    while True:
        observations=[(link.state(),after) for link,after in active]
        # Evaluate every arm against one clock after all I/O has completed.
        now=time.monotonic();confirmed=True
        for state,confirmed_after in observations:
            sample=(state or {}).get('latest') or {}
            if not state or state.get('error'):raise RuntimeError('정지 확인 중 상태 조회 실패')
            confirmed &= bool(sample.get('monotonic',0)>=confirmed_after and stopped(state,now=now))
        if confirmed and control.confirm_estop([s for s,_ in observations]):break
        if time.monotonic()-began>=timeout:raise RuntimeError('종료 중 최신 정지 상태 확인 시간 초과')
        time.sleep(.05)


def finish_controls(control,connections,*,timeout=3.):
    """Seal admission, then resolve accepted ESTOP from new arm telemetry."""
    with control.lock:
        control.shutdown.set()
        control.ending_confirmation_closed=True
        worker=control.ending_worker
    try:
        # Admission is sealed before joining; device connections stay open until
        # the confirmation owner exits. Never race a second confirmation pass.
        if worker is not None:worker.join()
        with control.lock:
            pending=any(r['command']=='estop' and r['state']=='pending' for r in control.receipts.values())
        if pending:confirm_connected_stop(control,connections,timeout=timeout)
    finally:
        control.fail_pending()


class Runner:
    def __init__(self, link, emit, control, timeout, peer=None, linear=None):
        self.link, self.peer, self.linear = link, peer, linear
        self.control = control
        self.analysis=getattr(control,'analysis',None)
        def record_emit(suffix):
            if self.analysis is not None:
                self.analysis.inspection(self.timing_arm,getattr(self,'inspection_records',[]),
                    getattr(self,'inspection_log',None),self.failure_context.get('phase'))
            emit(suffix)
        self.emit=record_emit if self.analysis is not None else emit
        self._analysis_motion=None
        self.execution_timeout=timeout
        self.deadline = time.monotonic() + timeout
        self.safe_ticks = None
        self.chain_start=None;self.prepared_start=None
        self.linear_active = False
        self.linear_target = None
        self.linear_until = None
        self.inspection_batch = None
        self.prepared_inspector = None
        self.on_work_started = None
        self.assembly_evidence = None
        self.inspection_jigs = {}
        self.adopted_jigs = None
        self.inspection_due = []
        self.inspection_started = set()
        self.inspection_passed = set()
        self.finished_return_step = None
        self.linear_return_started = False
        self.follower_phase = 'active'
        self.episode_camera_required = False
        self.camera_owned = False
        self.camera_preparing = False
        self.camera_prepare_started = None
        self.preparation_epoch = 0
        self.recovery=None;self.inspection_checkpoint=None
        self.execution_bundle=None;self.execution_steps=[];self.resume_configuration=None
        self.planned_epoch = None
        from failure_images import FailureImages
        self.failure_images=FailureImages(ROOT/'data')
        self.failure_context={'phase':'preparation',**(self.control.info or {})}
        self.failure_inspection=None;self.last_state=None;self.decision_evidence=None
        self.control.emergency = self.emergency
        self.timing=control.startup_timing
        self.timing_arm=getattr(link,'arm',(control.info or {}).get('arm','arm2'))
        self.preparation_active=True

    def preparation_call(self,name,function,*args,**kwargs):
        return self.timing.call(self.timing_arm+'.'+name,function,*args,**kwargs)

    def report_preparation(self):
        profile=self.timing.snapshot();profile['arm']=self.timing_arm
        if self.analysis is not None:self.analysis.preparation_profile(profile)
        profile['metrics']={k:v for k,v in profile['metrics'].items() if k.startswith((self.timing_arm+'.','shared.')) or '.load.' in k}
        profile['milestones']={k:v for k,v in profile['milestones'].items() if k.startswith(self.timing_arm+'.')}
        # Keep detailed spans once in analysis; ROS/human-readable feedback
        # carries the cumulative totals and an explicit reference only.
        profile.pop('intervals',None)
        profile['intervals_reference']='analysis.preparation_profiles.'+profile['profile_id']
        self.emit('PREPARATION_PROFILE:'+json.dumps(profile,ensure_ascii=False,separators=(',',':')))

    def camera_frame(self, body):
        reply=(self.preparation_call('camera.http',self.link.post,'/camera',body)
               if self.preparation_active else self.link.post('/camera',body))
        if self.preparation_active:
            d=reply.get('detection') or {};token=(reply.get('at'),d.get('detector_generation'),d.get('detector_sequence'))
            if reply.get('at') is not None:
                source='.camera.preparation_observed' if d.get('vision_ready') else '.jig'
                self.timing.sample(self.timing_arm+source,token,{k:d[k] for k in ('processing_seconds','model_seconds','opencv_seconds') if k in d})
            warm=d.get('model_preparation_timing') or {}
            if (self.camera_prepare_started is not None and type(warm.get('started')) in (int,float)
                    and warm['started']>=self.camera_prepare_started):
                self.timing.sample(self.timing_arm+'.camera',warm['started'],
                    {'model_warmup':warm.get('seconds'),'before_model_warmup':warm['started']-self.camera_prepare_started})
        try:self.failure_images.remember(reply)
        except Exception as exc:self.failure_images.capture_error=str(exc)
        return reply

    def prepare_camera(self):
        # The runtime's preparation processor warms AI without collecting poses.
        self.camera_prepare_started=time.monotonic()
        self.camera_owned=True
        self.preparation_call('camera.prepare_rpc',self.link.rpc,'camera_prepare')
        self.camera_preparing=True
        self.emit('VISION_PREPARING')

    def await_camera_ready(self):
        if not self.camera_preparing:self.prepare_camera()
        after_at=after_preview_at=None
        while True:
            self.check()
            if self.linear_active:self.poll_linear()
            frame=self.camera_frame({'after_at':after_at,'after_preview_at':after_preview_at})
            after_at,after_preview_at=frame.get('at'),frame.get('preview_at')
            detection=frame.get('detection') or {}
            error=frame.get('error') or detection.get('error')
            if error:raise RuntimeError('카메라·모델 준비 실패: '+str(error))
            now=time.monotonic();at=frame.get('at')
            if (detection.get('vision_ready') and type(at) in (int,float)
                    and self.camera_prepare_started<=at<=now and now-at<3):
                self.emit('VISION_READY')
                self.timing.add(self.timing_arm+'.camera.prepare_elapsed',now-self.camera_prepare_started)
                return
            if now-self.camera_prepare_started>=30:raise RuntimeError('카메라·모델 준비 시간 초과')
            time.sleep(.05)

    def pause_camera(self):
        if self.camera_owned:
            self.link.rpc('camera_mode',{'processing_enabled':False,'preview_fps':10})
        elif getattr(self.link,'keep_prepared',False):self.link.rpc('camera_mode',{'processing_enabled':False,'preview_fps':10})
        else:self.link.rpc('camera_stop')

    def close_episode_camera(self):
        if not self.camera_owned:return
        if getattr(self.link,'keep_prepared',False):
            self.link.rpc('camera_mode',{'processing_enabled':False,'preview_fps':10})
            self.camera_owned=False;self.camera_preparing=False;return
        self.camera_preparing=False
        try:
            self.link.rpc('camera_stop')
            until=time.monotonic()+3
            while self.link.post('/camera',{}).get('running'):
                if time.monotonic()>=until:raise RuntimeError('에피소드 카메라 해제 시간 초과')
                time.sleep(.02)
        finally:self.camera_owned=False

    def save_failure_images(self, error):
        # Called only after emergency/HOLD. Image I/O must never replace the
        # original failure, and it must not reconnect a device to obtain a photo.
        if self.failure_images.saved:return
        try:
            context={**self.failure_context,'last_follower_state':self.last_state,
                     'fixed_jigs':self.inspection_jigs,'linear_target_mm':self.linear_target,
                     'inspection_log':str(self.inspection_log) if hasattr(self,'inspection_log') else None}
            path=self.failure_images.save(error,context=context,inspection=self.failure_inspection)
            reference=str(path.relative_to(ROOT))
            self.emit('FAILURE_IMAGES:'+reference)
            if hasattr(self,'inspection_log') and self.inspection_log.exists():
                record=read_json(self.inspection_log);record['failure_evidence']=reference
                atomic_json(self.inspection_log,record)
        except Exception as exc:
            print('오류 영상 저장 실패: '+str(exc),file=sys.stderr)
            try:self.emit('FAILURE_IMAGE_SAVE_FAILED:'+str(exc))
            except Exception:pass

    def emergency(self):
        # Runs on the control thread too, so an IK/HTTP wait cannot defer HOLD.
        errors = []
        # L12-R has no verified stop input: arm HOLD never changes its target/timer.
        if self.link and self.link.lease:
            try:
                follower = self.link.state()
                if follower is None or not follower.get('running'): return errors
                self.link.post('/rpc', {'lease': self.link.lease, 'id': uuid.uuid4().hex,
                                       'method': 'command', 'args': {'action': 'hold'}})
            except Exception as exc: errors.append(str(exc))
        return errors

    def state(self):
        if self.control.shutdown.is_set(): raise ShutdownRequested('PC_COMPUTE_LOST:'+self.control.compute_error if getattr(self.control,'compute_error',None) else 'PROCESS_SHUTDOWN')
        for source in (self.peer, self.linear):
            if source and source.error: raise RuntimeError(source.error)
        state = self.link.state() if self.link else None
        self.last_state=state
        if self.analysis is not None:self.analysis.follower(self.timing_arm,state)
        if self.follower_phase=='before_connect' and (not state or not state.get('running')):
            return None
        if self.follower_phase=='connecting' and (not state or not state.get('running')):
            if state and state.get('error'):raise RuntimeError(state['error'])
            return None
        if state and (state.get('error') or not state['running']):
            raise RuntimeError(state.get('error') or '팔로워 연결 종료')
        return state

    def start_follower(self, bundle, speed):
        """Configure/connect only: never consume shared pause/reset controls."""
        self.preparation_call('follower.configure',self.link.rpc,'configure',{'bundle':bundle})
        if self.episode_camera_required:self.prepare_camera()
        if self.control.shutdown.is_set():raise ShutdownRequested('PC_COMPUTE_LOST:'+self.control.compute_error if getattr(self.control,'compute_error',None) else 'PROCESS_SHUTDOWN')
        started=time.monotonic()
        self.preparation_call('follower.connect',self.link.rpc,'connect',{'speed':speed})
        self.follower_phase='connecting'
        return started

    def finish_follower(self, started, speed):
        def ready(state):
            sample=(state or {}).get('latest') or {}
            return bool(state and state.get('running') and state['state'] in ('READ_ONLY','HOLD','ACTIVATION_PAUSED')
                        and not state.get('release_pending',False) and sample.get('calibration_matches') and started<=sample.get('monotonic',0)
                        and 0<=time.monotonic()-sample['monotonic']<.5)
        state=self.preparation_call('follower.first_sample',self.wait,ready,10)
        self.preparation_call('follower.speed',self.link.rpc,'speed',{'rate':speed})
        self.emit('FOLLOWER_READY')
        return state

    def prepare_follower(self, bundle, speed):
        """A retained closed session is not the new connection's result."""
        self.follower_phase='before_connect'
        try:
            self.check()
            self.emit('FOLLOWER_CONNECTING')
            started=self.start_follower(bundle,speed)
            return self.finish_follower(started,speed)
        finally:self.follower_phase='active'

    def poll_frozen_inspections(self):
        """Advance fixed evidence without camera I/O or starting linear motion."""
        if not self.inspection_batch:return True
        try:self.inspection_batch.poll(frozen_only=True)
        except Exception as exc:
            self.failure_context['phase']='paused_inspection'
            self.failure_inspection=getattr(exc,'inspection_snapshot',None)
            raise
        return not self.inspection_batch.pending

    def emit_pause_context(self):
        self.emit('RECOVERY_CONTEXT:'+json.dumps({'job_id':self.control.job_id,
            'stop_generation':self.control.stop_generation,'recheck_required':self.recovery is not None},separators=(',',':')))

    def await_pause(self):
        began = time.monotonic()
        self.emit('ESTOP_REQUESTED')
        errors = self.emergency()
        if errors: raise RuntimeError('정지 요청 실패: ' + '; '.join(errors))
        stop_state = None
        while True:
            stop_state = self.state()
            arm_ready = self.link is None or stop_state is None or stopped(stop_state)
            confirmed=arm_ready and self.control.confirm_estop([] if self.link is None or stop_state is None else [stop_state])
            if not self.control.reset.is_set():self.poll_frozen_inspections()
            if confirmed:break
            if time.monotonic() - began > 15: raise RuntimeError('정지 확인 시간 초과')
            time.sleep(.05)
        if self.linear_active and not self.poll_linear():
            self.emit('LINEAR_CONTINUES:ESTOP_APPLIES_TO_ARM_ONLY')
        with self.control.lock:
            self.preparation_epoch += 1
        self.emit_pause_context();self.emit('PAUSED')
        if self.link is None:
            # A slide-only preparation has no arm sequence to resume. Drain the
            # existing timed move, then terminate without reset or new PWM.
            while self.linear_active:
                self.state()
                if self.poll_linear(): break
                time.sleep(.05)
            raise ShutdownRequested('SLIDE_PREPARATION_CANCELLED')
        while True:
            state = self.state()
            if state and not stopped(state):
                raise RuntimeError('비상정지 중 자세 유지 상태 변경')
            if self.linear_active: self.poll_linear()
            with self.control.lock:
                self.control.consume_supervision_stop()
                stop_ready=self.control.confirm_estop([] if state is None else [state])
                if stop_ready and self.control.reset.is_set():
                    self.control.reset.clear(); self.control.pause.clear(); self.control.paused = False
                    self.deadline = time.monotonic() + 120
                    raise ResetRequested('RESET')
            # Draining the result also submits the next already captured image.
            # Do this throughout the pause, not only after RESTART is requested.
            inspections_ready=self.poll_frozen_inspections()
            with self.control.lock:
                self.control.consume_supervision_stop()
                stop_ready=self.control.confirm_estop([] if state is None else [state])
                if stop_ready and self.control.resume.is_set():
                    if self.inspection_batch:self.inspection_batch.restart()
                    if inspections_ready:
                        self.control.resume.clear(); self.control.pause.clear(); self.control.paused = False
                        self.control.complete('restart')
                        break
            time.sleep(.05)
        self.deadline += time.monotonic() - began
        self.emit('RESUMED')
        return stop_state

    def check(self):
        if self.recovery is not None and self.recovery.rechecking:return self.recovery.check()
        if self.inspection_checkpoint is not None and self.control.pause.is_set():
            raise InspectionRetry('체크포인트 검사 보류 · 새 영상 재검사 필요')
        paused = False
        while True:
            if self.control.pause.is_set():
                self.await_pause(); paused = True
            state = self.state()
            # A stop can arrive while the state request is in flight. Consume
            # that pause before treating the returned post-stop state as ready.
            if not self.control.pause.is_set():break
        if time.monotonic() >= self.deadline: raise RuntimeError('실행 제한시간 초과')
        return state, paused

    def wait(self, predicate, seconds=None):
        until = time.monotonic() + seconds if seconds else self.deadline
        while True:
            before = self.deadline
            state, resumed = self.check()
            until += self.deadline - before
            if resumed and predicate is settled and (activation_paused(state) or torque_off(state)):
                self.link.rpc('command',{'action':'arm'});continue
            if predicate(state): return state
            if time.monotonic() >= until: raise RuntimeError('팔로워 상태 확인 시간 초과')
            time.sleep(.05)

    @staticmethod
    def stage(name):
        for prefix, stage in (('하단', 'LOWER'), ('중단', 'MIDDLE'), ('상단', 'UPPER')):
            if name.startswith(prefix): return stage
        if name.startswith('완제품'):
            return 'PLACE' if '놓' in name else 'PICK'
        return None

    def move(self, action, targets, steps=None, completion_events=None, *, progress_offset=0, total_steps=None):
        if action=='play' and steps and self.inspection_batch:
            from so101_teach.episode_inspection import segments
            offset=progress_offset;total=total_steps or len(steps);state=None
            if completion_events is None:
                completion_events={self.stage(step['name']):step['id'] for step in steps if self.stage(step['name'])}
            for part in segments(steps,targets):
                state=self._tracked_move(action,part['targets'],part['steps'],completion_events,
                    progress_offset=offset,total_steps=total)
                offset+=len(part['steps'])
                if part['check'] is not None:
                    step=part['steps'][-1]
                    self.inspection_batch.start(step);self.inspection_started.add(step['id'])
                    # Capture at the confirmed pose, then classify only those
                    # frozen frames while the next movement segment proceeds.
                    self.inspection_checkpoint=deepcopy(state)
                    try:
                        try:self.capture_checkpoint()
                        except InspectionRetry as exc:self.recover_inspections(exc)
                    finally:self.inspection_checkpoint=None
            # A late result still gates successful completion and is recoverable
            # at the final confirmed pose without replaying completed movement.
            if self.inspection_batch.pending:
                self.inspection_checkpoint=deepcopy(state)
                try:self.retry_visual(self.wait_checkpoint,validate=self.recheck_checkpoint)
                finally:self.inspection_checkpoint=None
            return self.state()
        return self._tracked_move(action,targets,steps,completion_events,progress_offset=progress_offset,total_steps=total_steps)

    def configuration_state(self):
        if self.execution_bundle is None:return None
        from so101_teach.prepared_standby import configuration_stamp
        profile=self.execution_bundle.get('profile',{});arm=profile.get('robot_id','arm2')
        data=ROOT/('data' if arm=='arm2' else 'data/arm3-runtime')
        items=deepcopy(self.execution_bundle.get('jigs',{}))
        for item in items.values():
            if item.get('stl') and not Path(item['stl']).is_absolute():item['stl']=str(data/item['stl'])
        return configuration_stamp(SimpleNamespace(shared_root=lambda:ROOT,data_dir=data,profile=profile,
                                                   robot_id=arm,catalog=SimpleNamespace(items=items)))

    def retry_visual(self,operation,*,validate=None):
        try:return operation()
        except RetryableFault as exc:return self.recover_visual(exc,operation,validate=validate)

    def recover_visual(self,error,operation,*,validate=None):
        from episode_resume import FaultRecovery
        api=SimpleNamespace(stopped=stopped,confirm_connected_stop=confirm_connected_stop,ResetRequested=ResetRequested,ShutdownRequested=ShutdownRequested)
        return FaultRecovery(self,api,operation,validate).run(error)

    def recover_inspections(self,error):
        # During overlapped motion the old capture pose is no longer the resume
        # pose. FaultRecovery first confirms HOLD and guards that actual pose.
        return self.recover_visual(error,self.wait_checkpoint,validate=self.recheck_checkpoint)

    def capture_checkpoint(self):
        from episode_resume import PoseGuard
        guard=PoseGuard(self.inspection_checkpoint)
        while self.inspection_batch.capturing:
            state,_=self.check();guard.validate(state,stopped)
            if self.linear_active:self.poll_linear()
            self.poll_inspections()
            if self.inspection_batch.capturing:time.sleep(.05)

    def wait_checkpoint(self):
        from episode_resume import PoseGuard
        guard=PoseGuard(self.inspection_checkpoint or self.state())
        while self.inspection_batch.pending:
            state,_=self.check();guard.validate(state,stopped)
            if self.linear_active:self.poll_linear()
            self.poll_inspections()
            if self.inspection_batch.pending:time.sleep(.05)

    def recheck_checkpoint(self):
        from episode_resume import retained_jigs
        if self.execution_bundle is None:raise ResumeBlocked('현재 에피소드의 재검사 설정이 없습니다.')
        # The original path stays bound to the original adopted coordinates.
        # Only part/seat inspection needs fresh images after a pause.
        retained_jigs(self.execution_steps,self.inspection_jigs)
        self.prepare_camera();self.await_camera_ready()
        if self.linear_active and not self.poll_linear():raise ResumeBlocked('기존 리니어 구동 완료 확인 후 다시 재개하세요.')
        if self.linear is not None:
            stage=self.linear.call('status')
            if stage.get('phase')!='TIMED_COMPLETE' or stage.get('target_mm')!=self.linear_target:
                raise ResumeBlocked('검사 당시 리니어 위치와 현재 명령 상태가 다릅니다. RESET이 필요합니다.')
        self.emit('JIG_BASIS_REUSED')
        self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})
        self.inspection_after_at=self.inspection_after_preview=None
        self.inspection_batch.recapture()
        self.emit('INSPECTION_RECHECK_STARTED')

    def _tracked_move(self, action, targets, steps=None, completion_events=None, *, progress_offset=0, total_steps=None):
        if self.analysis is None:
            return self._move(action,targets,steps,completion_events,progress_offset=progress_offset,total_steps=total_steps)
        token=self.analysis.motion_start(self.timing_arm,action,targets);self._analysis_motion=token
        error=None
        try:return self._move(action,targets,steps,completion_events,progress_offset=progress_offset,total_steps=total_steps)
        except BaseException as exc:error=exc;raise
        finally:
            self.analysis.motion_end(token,error);self._analysis_motion=None

    def _move(self, action, targets, steps=None, completion_events=None, *, progress_offset=0, total_steps=None):
        # Only the first segment may request a preparation retry. Replaying a
        # whole episode after a later segment was paused would repeat work.
        preparing = action == 'play' and self.planned_epoch is not None and progress_offset == 0
        original = deepcopy(targets)
        offset, reported = 0, 0
        stage_ends = {}
        for i, step in enumerate(steps or []):
            if completion_events is None:
                stage = self.stage(step['name'])
                if stage: stage_ends[stage] = i + 1
            else:
                for stage,key in completion_events.items():
                    if key==step['id']:stage_ends[stage]=i+1
        def progress(count):
            nonlocal reported
            if not steps: return
            while reported < count:
                reported += 1
                self.failure_context.update(completed_step_id=steps[reported-1].get('id'),completed_step_name=steps[reported-1]['name'])
                if self.analysis is not None:self.analysis.step_completed(self.timing_arm,steps[reported-1],progress_offset+reported,total_steps or len(steps))
                self.emit(f"STEP_DONE:{progress_offset+reported}/{total_steps or len(steps)}:{steps[reported-1]['name']}")
                for stage, end in stage_ends.items():
                    if reported == end: self.emit('STAGE_DONE:' + stage)
        try:self.check()
        except InspectionRetry as exc:self.recover_inspections(exc)
        if preparing and self.planned_epoch != self.preparation_epoch:
            raise PreparationRestart('준비 이후 재개 · 경로 재계산 필요')
        self.failure_context.update(phase='motion',motion_action=action)
        if self.analysis is not None:self.analysis.motion_attempt(self._analysis_motion)
        request = self.link.rpc('command', {'action': action, 'targets': original})['request_id']
        if self.analysis is not None:self.analysis.motion_request(self._analysis_motion,request)
        resume_state=None
        while True:
            if self.control.pause.is_set():
                try:
                    resume_state = self.await_pause()
                    if self.inspection_batch:self.inspection_batch.restart()
                    if not self.poll_frozen_inspections():
                        raise RuntimeError('재개 전 검사 결과 확인 미완료: 이동하지 않았습니다.')
                except InspectionRetry as exc:
                    self.recover_inspections(exc);resume_state=self.state()
            if resume_state is not None:
                stopped_state,resume_state=resume_state,None
                if stopped_state.get('completed_request_id') == request:
                    progress(len(original))
                    return self.state()
                if stopped_state.get('request_id') != request:
                    raise RuntimeError('재시작할 실행 요청이 변경되었습니다.')
                count = stopped_state.get('index')
                if type(count) is not int or not 0 <= count <= len(original) - offset:
                    raise RuntimeError('재시작할 스텝 번호를 확인하지 못했습니다.')
                offset += count; progress(offset)
                self.check()
                if offset == len(original): return self.state()
                if self.analysis is not None:self.analysis.motion_attempt(self._analysis_motion,step_offset=offset)
                request = self.link.rpc('command', {'action': action, 'targets': original[offset:]})['request_id']
                if self.analysis is not None:self.analysis.motion_request(self._analysis_motion,request,step_offset=offset)
            state = self.state()
            if time.monotonic() >= self.deadline: raise RuntimeError('실행 제한시간 초과')
            if self.linear_active: self.poll_linear()
            if state and state.get('request_id') == request:
                count = state.get('index', 0)
                if type(count) is int: progress(min(len(original), offset + count))
            if self.inspection_batch:
                try:self.poll_inspections()
                except InspectionRetry as exc:
                    self.recover_inspections(exc);resume_state=self.state();continue
            if state and state.get('completed_request_id') == request and settled(state):
                progress(len(original)); return state
            # Unexpected HOLD (driver protection) must not look like a user pause.
            if state and state.get('request_id') == request and settled(state) and not self.control.pause.is_set():
                raise RuntimeError('드라이버 보호 정지: 자동 재개하지 않습니다.')
            time.sleep(.05)

    def start_linear(self, target, *, defer_pause=False):
        if defer_pause:
            # The motion loop owns pause/resume offsets. Never consume its pause
            # inside an inspection callback while the arm program is running.
            self.state()
            if time.monotonic() >= self.deadline:raise RuntimeError('실행 제한시간 초과')
            if self.control.pause.is_set():return False
        else:self.check()
        position='ASSEMBLY' if target == 100. else 'UNLOAD'
        self.emit(f'LINEAR_CHECK:target_mm={target:g}:target_position={position}')
        # Status/record publication may wait for disk I/O. Do not let a stop or
        # disconnect during that wait authorize a new carriage target afterward.
        while True:
            # The other arm's heartbeat may not have noticed lease loss yet.
            peer_state=None
            if self.peer:
                peer_state=self.peer.state()
                if peer_state and peer_state.get('error'):raise RuntimeError(peer_state['error'])
                if peer_state and not peer_state.get('running') and not (
                        normally_closed(peer_state) and peer_state==getattr(self.peer,'initial_inactive_follower',None)):
                    raise RuntimeError('리니어 시작 전 반대쪽 팔 연결 종료')
            if defer_pause:
                self.state()
                if time.monotonic() >= self.deadline:raise RuntimeError('실행 제한시간 초과')
                if self.control.pause.is_set():return False
            else:
                _,resumed=self.check()
                if resumed:continue  # Revalidate the peer after an arbitrary pause.
            if peer_state and peer_state.get('running'):
                sample=peer_state.get('latest') or {}
                if (not sample.get('calibration_matches') or len(sample.get('ticks',{}))!=6
                        or len(sample.get('telemetry',{}))!=6
                        or not 0<=time.monotonic()-sample.get('monotonic',0)<.5):
                    raise RuntimeError('리니어 시작 전 반대쪽 팔의 최신 모터 상태를 확인할 수 없습니다.')
            break
        value = self.linear.call('ensure', target_mm=target)
        self.linear_target = target
        self.linear_until = time.monotonic() + 12
        self.linear_active = value['phase'] != 'TIMED_COMPLETE'
        if not self.linear_active:
            self.emit(f'LINEAR_READY:target_mm={target:g}:target_position={position}:position_measured=false')
        else:
            self.emit(f'LINEAR_STARTED:target_mm={target:g}:target_position={position}')
        return True

    def poll_linear(self):
        if not self.linear_active:
            return True
        value = self.linear.call('status')
        if value['target_mm'] != self.linear_target:
            raise RuntimeError('리니어 목표가 변경되었습니다.')
        if value['phase'] == 'TIMED_COMPLETE':
            self.linear_active = False
            position='ASSEMBLY' if self.linear_target == 100. else 'UNLOAD'
            self.emit(f'LINEAR_DONE:TIME_BASED:target_mm={self.linear_target:g}:target_position={position}:position_measured=false')
            return True
        if value['phase'] != 'MOVING':
            raise RuntimeError(value.get('error') or '리니어 실행 상태 오류: ' + value['phase'])
        if time.monotonic() > self.linear_until:
            raise RuntimeError('리니어 완료 대기 시간 초과')
        return False

    def wait_linear(self):
        resumed = False
        while True:
            _, was_resumed = self.check()
            resumed = resumed or was_resumed
            if self.poll_linear():
                return resumed
            time.sleep(.05)

    def prepare_linear(self, target):
        self.start_linear(target)
        self.wait_linear()

    def prepare_work(self, steps, bundle, target, startup_checks=(), *, linear_started=False, on_measured=None):
        # Fixed jigs can be measured while the already-started linear stage moves.
        if not linear_started:self.start_linear(target)
        if startup_checks:
            return self.prepare_startup(steps,bundle,startup_checks,on_measured=on_measured)
        while True:
            current = self.measure([*steps,*startup_checks], bundle)
            self.inspection_jigs=deepcopy(current)
            if on_measured is not None:on_measured(current)
            deferred=[];inspection_resumed=False
            if startup_checks:
                self.emit('STARTUP_INSPECTION_STARTED')
                self.poll_linear()
                deferred=[s for s in startup_checks if self.linear_active and s['inspection']['station']=='linear']
                immediate=[s for s in startup_checks if s not in deferred]
                if immediate:inspection_resumed=bool(self.inspect_startup_group(immediate,bundle))
            resumed = self.wait_linear()
            if deferred:inspection_resumed=bool(self.inspect_startup_group(deferred,bundle)) or inspection_resumed
            resumed=resumed or inspection_resumed
            _, resumed_again = self.check()
            if not resumed and not resumed_again:
                break
            self.emit('PREPARATION_RECHECK_AFTER_RESUME')
        self.emit('WORK_READY:LINEAR_COMPLETE_AND_JIG_CONFIRMED')
        if startup_checks:self.emit('STARTUP_INSPECTION_PASSED')
        return current

    def prepare_startup(self,steps,bundle,startup_checks,*,on_measured=None):
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_placement import load_placement
        from so101_teach.startup_inspection import StartupInspection
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        from so101_teach.execution_resources import execution_catalog
        catalog=execution_catalog(data,bundle['profile']);placement=load_placement(ROOT/'data')
        pipeline=None
        def begin():
            return StartupInspection(startup_checks,bundle['profile'],catalog,placement,
                self.prepared_inspector,self.emit,linear_target=self.linear_target,timing=self.timing,timing_prefix=self.timing_arm+'.inspection')
        def progress(current,frame=None,*,resumed=False):
            nonlocal pipeline
            if resumed:
                pipeline.close();pipeline=begin()
                self.emit('INSPECTION_GROUP_RESTARTED')
            # Main thread owns the device and all readiness checks.
            # Camera/linear I/O can outlive the earlier .5-second snapshot.
            # Re-read the follower here; stale telemetry still fails closed.
            state=self.state()
            if not (settled(state) or torque_off(state)):
                sample=(state or {}).get('latest') or {}
                age=time.monotonic()-sample.get('monotonic',0)
                if not 0<=age<.5:
                    raise RuntimeError(f'모터 상태 갱신 지연: {age:.2f}초 (기준 0.5초 미만)')
                raise RuntimeError('시작 전 검사 중 자세 유지 상태 변경')
            # Even a fixed-jig-only check needs the current linear position to
            # distinguish parts on neighbouring fixtures without guessing it.
            linear=self.linear.call('status') if self.linear else None
            pipeline.poll(current,frame,linear_state=linear)
        try:
            while True:
                pipeline=begin();epoch=self.preparation_epoch
                self.emit('STARTUP_INSPECTION_STARTED')
                current=self.measure([*steps,*startup_checks],bundle,on_progress=progress)
                epoch=self.preparation_epoch
                self.inspection_jigs=deepcopy(current)
                if on_measured is not None:on_measured(current)
                self.preparation_call('camera.preview_mode',self.link.rpc,'camera_start',{'processing_enabled':False,'preview_fps':10})
                after_at=after_preview=None;restart=False
                while True:
                    state,resumed=self.check()
                    if resumed or self.preparation_epoch!=epoch:
                        restart=True;break
                    if self.linear_active:self.poll_linear()
                    frame=self.camera_frame({'after_at':after_at,'after_preview_at':after_preview})
                    progress(current,frame)
                    after_at,after_preview=frame.get('at'),frame.get('preview_at')
                    if pipeline.complete and not self.linear_active:break
                    time.sleep(.05)
                if restart:
                    pipeline.close();pipeline=None
                    self.emit('PREPARATION_RECHECK_AFTER_RESUME');continue
                # Only now publish PASS: every provisional result still refers
                # to the final frozen jig snapshot and completed linear target.
                for step in pipeline.passed.values():
                    self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],
                        'check':deepcopy(step['inspection']),'product':step['inspection_product'],
                        'result':'PASS','at':time.time(),
                        **({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
                    if self.assembly_evidence:self.assembly_evidence.record(step,step['inspection_product'])
                    self.emit('INSPECTION_PASS:'+step['id'])
                atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,
                    'product_type':self.inspection_product,'checks':self.inspection_records})
                self.emit('WORK_READY:LINEAR_COMPLETE_AND_JIG_CONFIRMED')
                self.emit('STARTUP_INSPECTION_PASSED')
                return current
        except Exception as exc:
            if not isinstance(exc,(ResetRequested,RetryableFault,RecheckInterrupted)):self.control.begin_ending((self.link,self.peer))
            self.emergency()
            self.failure_inspection={'steps':deepcopy(startup_checks),
                'result':deepcopy(pipeline.observed if pipeline else None),'result_role':'last_consumed_inspection_result'}
            self.inspection_records.append({'result':'FAIL','error':str(exc),'at':time.time()})
            atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,
                'product_type':self.inspection_product,'checks':self.inspection_records})
            if isinstance(exc,RecheckInterrupted):self.emit('RECOVERY_RECHECK_CANCELLED')
            else:self.emit(('INSPECTION_RETRY_REQUIRED:' if isinstance(exc,RetryableFault) else 'INSPECTION_FAILED:')+str(exc))
            raise
        finally:
            if pipeline:self.preparation_call('inspection.close',pipeline.close)
            self.preparation_call('camera.pause',self.pause_camera)

    def drain_failed_plans(self,futures,*,ending):
        # Keep the already running read-only RPC bounded by its existing timeout.
        # Its executor must not hide new stop requests while waiting for exit.
        if ending:
            self.control.begin_ending((self.link,self.peer))
        for future in futures:future.cancel()
        while any(not future.done() for future in futures):
            with self.control.lock:
                pending=any(r['command']=='estop' and r['state']=='pending' for r in self.control.receipts.values())
            if pending and not ending:
                try:confirm_connected_stop(self.control,(self.link,self.peer))
                except Exception as exc:
                    # Unconfirmed stops remain errors; preserve the original job failure.
                    try:self.control.fail_pending()
                    except Exception as error:print('제어 완료 기록 정리 실패: '+str(error),file=sys.stderr)
                    print('계산 정리 중 정지 확인 실패: '+str(exc),file=sys.stderr)
            time.sleep(.05)

    def prepare_execution_plan(self, steps, bundle, target, startup_checks):
        # Only read-only IK runs in the worker. All readiness gates and motion
        # remain on this thread; a preparation pause retains adopted jig coordinates.
        with ThreadPoolExecutor(max_workers=1,thread_name_prefix='episode-plan') as pool:
            future=None;snapshot=None;futures=[]
            def begin(current):
                nonlocal future,snapshot
                if future is not None:future.cancel()
                snapshot=(deepcopy(steps),deepcopy(bundle),deepcopy(current),self.preparation_epoch)
                self.emit('PLAN_STARTED')
                future=pool.submit(self.preparation_call,'planning.rpc',self.link.rpc,'plan_snapshot',{'steps':snapshot[0],'current':snapshot[2]})
                futures.append(future)
            try:
                while True:
                    current=self.prepare_work(steps,bundle,target,startup_checks,linear_started=True,on_measured=begin)
                    self.failure_context['phase']='planning'
                    while not future.done() and snapshot[3]==self.preparation_epoch:
                        self.check()
                        if snapshot[3]!=self.preparation_epoch:break
                        try:self.preparation_call('planning.wait_after_preparation',future.result,timeout=.05)
                        except PlanTimeout:pass
                    self.check();self.wait(settled,15)
                    if snapshot!=(steps,bundle,current,self.preparation_epoch):
                        self.emit('PLAN_DISCARDED:PREPARATION_CHANGED');continue
                    plan=future.result()['plan']
                    self.check()
                    if snapshot[3]!=self.preparation_epoch:
                        self.emit('PLAN_DISCARDED:PREPARATION_CHANGED');continue
                    self.planned_epoch=self.preparation_epoch
                    if self.analysis is not None:self.analysis.plan(self.timing_arm,steps,plan)
                    self.emit('PLAN_READY')
                    return plan
            except BaseException as exc:
                self.drain_failed_plans(futures,ending=not isinstance(exc,(ResetRequested,RetryableFault,RecheckInterrupted)))
                raise

    def measure(self, steps, bundle, *, on_progress=None):
        if self.adopted_jigs is not None:
            from episode_resume import retained_jigs
            current=retained_jigs(steps,self.adopted_jigs)
            self.check()
            self.emit('JIG_BASIS_REUSED')
            if on_progress:on_progress(current)
            return current
        self.failure_context['phase']='jig_detection'
        from so101_teach.episode_inspection import inspection_jig_ids
        keys = {s['jig_id'] for s in steps if s.get('jig_id')} | inspection_jig_ids(steps)
        if not keys:
            self.adopted_jigs={}
            if on_progress:on_progress({})
            return {}
        self.link.rpc('detect_freeze', {'enabled': False})
        if self.camera_owned:self.preparation_call('camera.ready_wait',self.await_camera_ready)
        def begin():
            context={k:self.failure_context.get(k) for k in ('run_id','job_id','arm','episode_id','episode_name','product_type')}
            args={'decision_evidence':context} if self.decision_evidence is not None else None
            reply=self.link.rpc('detect_clear',args) if args is not None else self.link.rpc('detect_clear')
            if args is not None and isinstance(reply,dict):
                reference=reply.get('decision_evidence')
                if reference:
                    self.failure_context.setdefault('jig_decision_evidence',[]).append(reference)
                    if self.analysis is not None:self.analysis.decision_evidence(reference)
                if reply.get('evidence_error'):self.failure_context['jig_evidence_error']=reply['evidence_error']
            self.emit('JIG_DETECTION_STARTED')
            return time.monotonic()
        started = begin()
        if on_progress:on_progress({})
        until = started + bundle['acquisition_seconds'] * bundle['acquisition_attempts']
        self.preparation_call('camera.detection_mode',self.link.rpc,'camera_start',{'processing_enabled':True,'preview_fps':10})
        self.camera_preparing=False
        after_at = after_preview_at = None
        detected = set()
        try:
            while True:
                _, resumed = self.check()
                if self.linear_active: self.poll_linear()
                if resumed:
                    if self.camera_owned:self.await_camera_ready()
                    started = begin(); detected.clear()
                    until = started + bundle['acquisition_seconds'] * bundle['acquisition_attempts']
                    self.link.rpc('camera_start', {'processing_enabled': True, 'preview_fps': 10})
                    self.camera_preparing=False
                    if on_progress:on_progress({},resumed=True)
                frame = self.camera_frame({'after_at': after_at, 'after_preview_at': after_preview_at})
                after_at, after_preview_at = frame.get('at'), frame.get('preview_at')
                if frame.get('error'): raise RuntimeError(frame['error'])
                now = time.monotonic(); results = frame.get('detection', {}).get('by_jig', {}); current = {}
                if 0 <= now - frame.get('at', 0) < 3 and frame.get('at', 0) >= started:
                    live = frame.get('detection', {}).get('live_by_jig', results)
                    for key in keys:
                        if key not in detected and (live.get(key, {}).get('selected') or live.get(key, {}).get('candidates')):
                            self.emit('JIG_DETECTED:' + key); detected.add(key)
                            self.timing.mark(self.timing_arm+'.jig.first_candidate.'+key)
                        result = results.get(key, {}); metric = (result.get('selected') or {}).get('metric')
                        measured = result.get('pose_measured_at')
                        if (metric and measured is not None and started <= measured <= now
                                and now - measured < bundle['hold_seconds']
                                and result.get('acquisition_completed_attempts', 0) <= bundle['acquisition_attempts']):
                            current[key] = {'pose': [*metric['center_xy_mm'], metric['yaw_deg']],
                                'symmetry_deg': metric.get('symmetry_deg', 90), 'stl_sha256': bundle['mesh_hashes'][key],
                                **({'mesh_yaw_offset_deg': metric['mesh_yaw_offset_deg']} if metric.get('mesh_yaw_offset_deg') else {})}
                if self.decision_evidence is not None:
                    reference=frame.get('detection',{}).get('decision_evidence') or {'session':None,'frame_id':None}
                    self.decision_evidence.note(reference,{'kind':'jig_consumer','checked_at':now,
                        'captured_at':frame.get('at'),'started':started,'deadline':until,
                        'fresh':0<=now-frame.get('at',0)<3 and frame.get('at',0)>=started,
                        'required_jigs':sorted(keys),'adopted_jigs':current,
                        'missing_jigs':sorted(keys-current.keys()),'decision':('confirmed' if keys==current.keys() else 'timeout' if now>=until else 'waiting'),
                        'by_jig':results})
                if on_progress:on_progress(current,frame)
                if keys == current.keys():
                    self.preparation_call('jig.freeze_rpc',self.link.rpc,'detect_freeze',{'enabled':True})
                    self.failure_images.confirm_jigs(current)
                    if self.analysis is not None:self.analysis.poses(self.timing_arm,current,results,self.linear_target)
                    for key in sorted(keys):
                        self.emit('JIG_CONFIRMED:' + key)
                        self.timing.mark(self.timing_arm+'.jig.confirmed.'+key)
                        x, y, yaw = current[key]['pose']
                        self.emit(f'JIG_POSE:{key}:x_mm={x:.2f}:y_mm={y:.2f}:yaw_deg={yaw:.2f}')
                    self.adopted_jigs=deepcopy(current)
                    return current
                if now >= until: raise JigRetry('지그 측정 실패: ' + ', '.join(sorted(keys - current.keys())))
                time.sleep(.1)
        finally:
            self.pause_camera()

    def inspect_checkpoint(self,step,bundle,*,startup=False):
        import base64
        import cv2
        import numpy as np
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_placement import load_placement
        from so101_teach.episode_inspection import InspectionDecision
        from so101_teach.episode_inspection_runtime import FrameInspection
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        placement=load_placement(ROOT/'data')
        if placement and self.linear_target is not None:
            placement['linear_stage']['startup_state']={'known':not self.linear_active,'commanded_mm':self.linear_target,'moving':self.linear_active}
        worker=None;observed=None
        self.failure_context['phase']='checkpoint_inspection'
        def begin():
            product=step.get('inspection_product',self.inspection_product)
            return FrameInspection(data,bundle['profile'],catalog,placement,step['inspection'],product,fixed_jigs=self.inspection_jigs,prepared_inspector=self.prepared_inspector,visual_step=step),InspectionDecision(step['inspection'],product,time.monotonic(),evidence=self.assembly_evidence)
        try:
            if startup and step['inspection']['station']=='linear':
                if not self.linear:raise RuntimeError('시작 전 검사: 리니어 제어기 상태를 확인할 수 없습니다.')
                linear=self.linear.call('status')
                if linear.get('phase')!='TIMED_COMPLETE' or type(linear.get('target_mm')) not in (int,float):raise RuntimeError('시작 전 검사: 리니어 정지·명령 위치 확인 필요')
                if not placement:raise RuntimeError('시작 전 검사: 작업대 좌표 없음')
                placement['linear_stage']['startup_state']={'known':True,'commanded_mm':linear['target_mm'],'moving':False}
            catalog=JigCatalog(data,profile=bundle['profile'])
            worker,decision=begin();self.emit('INSPECTION_STARTED:'+step['id']+':'+step['name'])
            self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})
            after_at=after_preview=None
            while True:
                state,resumed=self.check()
                if not (settled(state) or startup and torque_off(state)):raise RuntimeError('안착 검사 중 자세 유지 상태 변경')
                if startup and step['inspection']['station']=='linear':
                    linear=self.linear.call('status');expected=placement['linear_stage']['startup_state']['commanded_mm']
                    if linear.get('phase')!='TIMED_COMPLETE' or linear.get('target_mm')!=expected:raise RuntimeError('시작 전 검사 중 리니어 상태 변경')
                if resumed:worker.close();worker,decision=begin();after_at=after_preview=None
                frame=self.camera_frame({'after_at':after_at,'after_preview_at':after_preview})
                if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
                image=frame.get('preview_image') or frame.get('image')
                at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
                now=time.monotonic()
                if image and type(at) in (int,float) and decision.started<=at<=now and now-at<1:
                    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
                    if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                    worker.submit(decoded,at)
                after_at,after_preview=frame.get('at'),frame.get('preview_at')
                observed=worker.result
                if decision.observe(observed,now):
                    if decision.basis:step={**step,'inspection_basis':deepcopy(decision.basis)}
                    if self.assembly_evidence:self.assembly_evidence.record(step,decision.product)
                    self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
                    atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                    self.emit('INSPECTION_PASS:'+step['id']);return
                time.sleep(.05)
        except Exception as exc:
            self.emergency()
            self.failure_inspection={'steps':[deepcopy(step)],'result':deepcopy(observed),'result_role':'last_consumed_inspection_result'}
            self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'result':'FAIL','error':str(exc),'at':time.time()})
            try:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            finally:self.emit('INSPECTION_FAILED:'+step['id']+':'+str(exc))
            raise
        finally:
            if worker:worker.close()
            self.pause_camera()

    def inspect_startup_group(self,steps,bundle):
        import base64,cv2,numpy as np
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_placement import load_placement
        from so101_teach.episode_inspection import GroupInspectionDecision
        from so101_teach.episode_inspection_runtime import FrameInspections
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        placement=load_placement(ROOT/'data');catalog=JigCatalog(data,profile=bundle['profile'])
        worker=None;linear_target=None;counts={};was_resumed=False;observed=None
        self.failure_context['phase']='startup_inspection'
        try:
            if any(s['inspection']['station']=='linear' for s in steps):
                state=self.linear.call('status') if self.linear else {}
                if state.get('phase')!='TIMED_COMPLETE' or type(state.get('target_mm')) not in (int,float):raise RuntimeError('시작 전 검사: 리니어 정지·명령 위치 확인 필요')
                if not placement:raise RuntimeError('시작 전 검사: 작업대 좌표 없음')
                linear_target=state['target_mm'];placement['linear_stage']['startup_state']={'known':True,'commanded_mm':linear_target,'moving':False}
            def begin():
                decision=GroupInspectionDecision(steps,time.monotonic(),evidence=self.assembly_evidence)
                return FrameInspections(data,bundle['profile'],catalog,placement,steps,fixed_jigs=self.inspection_jigs,prepared_inspector=self.prepared_inspector),decision
            worker,decision=begin()
            self.emit('INSPECTION_GROUP_STARTED:'+str(len(steps)))
            for step in steps:self.emit('INSPECTION_STARTED:'+step['id']+':'+step['name'])
            self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})
            after_at=after_preview=submitted=inflight=None
            while True:
                state,resumed=self.check()
                if not (settled(state) or torque_off(state)):raise RuntimeError('시작 전 검사 중 자세 유지 상태 변경')
                if linear_target is not None:
                    stage=self.linear.call('status')
                    if stage.get('phase')!='TIMED_COMPLETE' or stage.get('target_mm')!=linear_target:raise RuntimeError('시작 전 검사 중 리니어 상태 변경')
                if resumed:
                    was_resumed=True
                    worker.close();worker,decision=begin();after_at=after_preview=submitted=inflight=None;counts.clear()
                    self.emit('INSPECTION_GROUP_RESTARTED')
                frame=self.camera_frame({'after_at':after_at,'after_preview_at':after_preview})
                if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
                image=frame.get('preview_image') or frame.get('image')
                at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
                now=time.monotonic()
                after_at,after_preview=frame.get('at'),frame.get('preview_at')
                observed=worker.result
                passed=decision.observe(observed,now)
                if observed and observed.get('at')==inflight:inflight=None
                for step in passed:
                    self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'product':step['inspection_product'],'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
                    self.emit('INSPECTION_PASS:'+step['id'])
                if passed:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                for key,d in decision.pending.items():
                    if counts.get(key)!=d.count:
                        counts[key]=d.count;self.emit('INSPECTION_COUNT:'+key+f":{d.count}/{d.check.get('minimum_observations',2)}")
                worker.retain(decision.pending)
                if not decision.pending:return was_resumed
                if (inflight is None and image and type(at) in (int,float) and 0<=now-at<1
                        and (submitted is None or at>submitted) and worker.accepting_frame
                        and any(d.started<=at for d in decision.pending.values())):
                    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
                    if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                    if worker.submit(decoded,at) is not False:submitted=at;inflight=at
                time.sleep(.05)
        except Exception as exc:
            self.emergency()
            self.failure_inspection={'steps':deepcopy(steps),'result':deepcopy(observed),'result_role':'last_consumed_inspection_result'}
            self.inspection_records.append({'result':'FAIL','error':str(exc),'at':time.time()})
            try:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            finally:self.emit('INSPECTION_FAILED:'+str(exc))
            raise
        finally:
            if worker:worker.close()
            self.pause_camera()

    def begin_inspections(self,steps,bundle,events):
        from so101_teach.episode_inspection import inspection_schedule
        from so101_teach.episode_inspection_runtime import InspectionBatch,FrameInspection
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_placement import load_placement
        self.inspection_due=inspection_schedule(steps);self.inspection_started=set()
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        catalog=JigCatalog(data,profile=bundle['profile']);placement=load_placement(ROOT/'data')
        if placement and self.linear_target is not None:
            placement['linear_stage']['startup_state']={'known':not self.linear_active,'commanded_mm':self.linear_target,'moving':self.linear_active}
        def worker(step):
            self.emit('INSPECTION_STARTED:'+step['id']+':'+step['name'])
            return FrameInspection(data,bundle['profile'],catalog,placement,step['inspection'],self.inspection_product,fixed_jigs=self.inspection_jigs,prepared_inspector=self.prepared_inspector,visual_step=step,preserve_pending_frames=True)
        def passed(step):
            self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'result':'PASS','at':time.time(),'capture':deepcopy(step.get('inspection_capture')),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
            atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            self.emit('INSPECTION_PASS:'+step['id'])
            self.inspection_passed.add(step['id'])
            for stage,key in events.items():
                if key==step['id']:self.emit('STAGE_DONE:'+stage)
        self.inspection_batch=InspectionBatch(self.inspection_product,worker,passed,evidence=self.assembly_evidence)
        self.inspection_after_at=self.inspection_after_preview=None
        self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})

    def poll_inspections(self):
        if not self.inspection_batch:return
        if not self.inspection_batch.pending:
            self.start_finished_return();return
        import base64,cv2,numpy as np
        # Read the already captured preview; the native classifier runs in its worker.
        frame=self.camera_frame({'after_at':self.inspection_after_at,'after_preview_at':self.inspection_after_preview}) if self.inspection_batch.capturing else {}
        if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
        image=frame.get('preview_image') or frame.get('image')
        at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
        view=None;now=time.monotonic()
        if image and type(at) in (int,float) and 0<=now-at<1:
            decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
            if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
            view=(decoded,at)
        self.inspection_after_at,self.inspection_after_preview=frame.get('at'),frame.get('preview_at')
        try:self.inspection_batch.poll(view)
        except Exception as exc:
            self.failure_context['phase']='motion_inspection'
            self.failure_inspection=getattr(exc,'inspection_snapshot',None)
            raise
        self.start_finished_return()

    def start_finished_return(self, *, defer_pause=True):
        if self.recovery is not None and self.recovery.rechecking:return
        # Finish earlier linear checks at the unload position before moving their
        # inspection target. The finished-pallet PASS can overlap arm safe return.
        linear_checks = {step['id'] for _, step in self.inspection_due
                         if step['inspection']['station'] == 'linear'}
        if (self.finished_return_step in self.inspection_passed
                and not self.linear_return_started
                and linear_checks <= self.inspection_passed):
            if not self.start_linear(100.,defer_pause=defer_pause):return
            self.linear_return_started = True
            self.emit('CHAIN_RETURN_STARTED:target_mm=100:target_position=ASSEMBLY')

    def execute(self, episode, bundle, speed, target):
        from so101_teach.episode_inspection import startup_steps
        self.episode_camera_required=any(s.get('jig_id') or s.get('inspection') for s in [*episode['steps'],*startup_steps(episode)])
        try:return self._execute(episode,bundle,speed,target)
        finally:
            failing=sys.exc_info()[0] is not None
            if failing and not isinstance(sys.exc_info()[1],ResetRequested):
                self.control.begin_ending((self.link,self.peer))
            # Do not put camera RPC cleanup ahead of HOLD for a motion failure.
            if failing and self.failure_context.get('phase')=='motion':self.emergency()
            try:
                try:
                    if self.prepared_inspector is not None:
                        self.prepared_inspector.close();self.prepared_inspector=None
                finally:
                    try:self.close_episode_camera()
                    finally:
                        if self.decision_evidence is not None:
                            try:
                                result=self.decision_evidence.close()
                                self.failure_context['decision_evidence_status']={k:result[k] for k in ('complete','pending_bytes','dropped_frames','errors')}
                            except Exception as exc:self.failure_context['decision_evidence_error']=str(exc)
                            self.decision_evidence=None
            except Exception as exc:
                if not failing:raise
                print('에피소드 카메라 정리 실패: '+str(exc),file=sys.stderr)
            if failing and self.preparation_active:
                try:self.report_preparation()
                except Exception:pass  # Diagnostic output must preserve the original failure.

    def prepare_run_inspections(self,episode,bundle,speed,target):
        self.timing_arm=bundle.get('profile',{}).get('robot_id',self.timing_arm)
        if self.analysis is not None:self.analysis.context(self.timing_arm,episode,bundle,speed,target,self.execution_timeout)
        self.failure_context.update(episode_id=episode.get('id'),episode_name=episode.get('name'),product_type=episode.get('product_type'),profile=deepcopy(bundle.get('profile')),jigs=deepcopy(bundle.get('jigs')),mesh_hashes=deepcopy(bundle.get('mesh_hashes')))
        if self.decision_evidence is None:
            from so101_teach.decision_evidence import start
            self.decision_evidence,error=start(ROOT/'data',self.failure_context,stream='inspection')
            if error:self.failure_context['decision_evidence_error']=error
            else:
                reference=self.decision_evidence.reference();self.failure_context['decision_evidence']=reference
                if self.analysis is not None:self.analysis.decision_evidence(reference)
        from so101_teach.episode_inspection import validate_inspection,has_inspections
        validate_inspection(episode)
        from so101_teach.episode_inspection import inspection_schedule,startup_steps
        inspection_schedule(episode['steps'])
        if self.analysis is not None:self.analysis.expect_inspections(self.timing_arm,[*startup_steps(episode),*episode['steps']])
        if has_inspections(episode):
            from so101_teach.assembly_evidence import AssemblyEvidence,geometry_scope
            from so101_teach.workcell_placement import load_placement
            self.assembly_evidence=AssemblyEvidence(ROOT/'data/assembly-evidence.json',scope=geometry_scope(load_placement(ROOT/'data')),
                episode_id=episode['id'])
            from so101_teach.episode_inspection_runtime import PreparedInspector
            reused_inspector=self.prepared_inspector is not None
            if self.prepared_inspector is None:
                if getattr(self.link,'keep_prepared',False):
                    from so101_teach.prepared_standby import RemotePreparedInspector
                    self.prepared_inspector=RemotePreparedInspector(self.link,bundle['profile'])
                else:self.prepared_inspector=PreparedInspector(bundle['profile'],timing=self.timing,timing_prefix=self.timing_arm+'.inspection',**({'compute':self.link.compute} if getattr(self.link,'compute',None) is not None else {}))
            if self.analysis is not None:self.analysis.resource_prepared(self.timing_arm,'inspection_model',
                'existing_instance_reused' if reused_inspector else 'creation_requested')
            self.prepared_inspector.evidence=self.decision_evidence
            from work_visuals import WorkVisuals
            self.prepared_inspector.visual=WorkVisuals(ROOT/'data',arm=self.timing_arm,
                job_id=self.control.job_id,run_id=(self.control.info or {}).get('run_id'),episode_id=episode['id'])
            self.emit('INSPECTION_MODEL_PREPARING')
            self.inspection_product=episode['product_type'];self.inspection_episode=episode['id'];self.inspection_records=[]
            self.inspection_log=ROOT/'data/diagnostics'/f'episode-inspection-{time.time_ns()}.json'
            atomic_json(self.inspection_log,{'episode_id':episode['id'],'product_type':self.inspection_product,'checks':[]})

    def prepare_safe_start(self,episode,bundle,speed,target):
        last = episode['steps'][-1]
        self.safe_ticks = deepcopy(last['ticks']) if last.get('safe_boundary') == 'end' and not last.get('jig_id') else None
        state = self.prepare_follower(bundle,speed) if self.chain_start is None else None
        self.emit('ACCEPTED')
        if self.chain_start is not None and 'receipts' not in self.chain_start:
            from episode_recovery import prepare_chain_start
            self.emit('CHAIN_SAFE_STARTED')
            emit=self.emit
            def report(event):
                if event in ('PAUSED','RESUMED'):emit(event)
                elif event.startswith('ARM_RESET_STARTED:'):emit('CHAIN_SAFE_ARM_STARTED:'+event.split(':')[1])
                elif event.startswith('ARM_RESET_DONE:'):emit('CHAIN_SAFE_ARM_DONE:'+event.split(':')[1])
                elif event.startswith('ARM_SAFE_ALREADY:'):emit('CHAIN_SAFE_ARM_SKIPPED:'+event.split(':')[1])
                elif event.startswith('ARM_RESET:arm2:LINEAR_'):emit(event.split(':',2)[2])
                else:emit('CHAIN_SAFE_DETAIL:'+event)
            saved_safe=self.safe_ticks
            try:
                receipts=prepare_chain_start(recovery_context(),self.chain_start['records'],self.chain_start['links'],
                    self.linear,self.control,report,active=self,speed=speed,
                    on_started=lambda:self.start_linear(target,defer_pause=True))
            finally:self.safe_ticks=saved_safe
            self.chain_start['receipts']=receipts;self.prepared_start=receipts['arm2']
            self.emit('CHAIN_SAFE_DONE')
        steps = deepcopy(episode['steps'])
        state, _ = self.check()
        if self.prepared_start is not None:
            prepared=self.prepared_start
            position_ok=not prepared.get('confirmed_without_move') or at_safe_pose(state,prepared['ticks'])
            completed=prepared.get('completed_request_id',prepared['request_id'])
            if (steps[0].get('safe_boundary')!='start' or steps[0].get('jig_id') or steps[0]['ticks']!=prepared['ticks']
                    or not position_ok or not settled(state) or state.get('request_id')!=prepared['request_id']
                    or state.get('completed_request_id')!=completed):
                raise RuntimeError('함께 준비한 시작 안전자세가 변경되었습니다. 연속 실행을 다시 준비하세요.')
        if state['state'] in ('READ_ONLY','ACTIVATION_PAUSED'): self.link.rpc('command', {'action': 'arm'})
        self.wait(settled, 15)
        # User verified the initial safe path does not intersect the linear path.
        # Model warm-up runs in the camera worker; no jig pose is adopted yet.
        if self.chain_start is None:self.start_linear(target)
        if steps[0].get('safe_boundary') == 'start':
            self.emit('SAFE_POSE_STARTED')
            first=steps.pop(0)
            if self.prepared_start is None:
                current,_=self.check()
                if at_safe_pose(current,first['ticks']):self.emit('SAFE_POSE_SKIPPED:ALREADY_AT_TARGET')
                else:self.move('move', [first['ticks']])
            else:self.emit('SAFE_POSE_SKIPPED:PREPARED_START_VALIDATED')
            self.emit('SAFE_POSE_REACHED')
        return steps

    def play_plan(self,episode,steps,bundle,plan):
        if any('inspection' in step for step in steps):
            events=episode.get('completion_events')
            if events is None:
                events={}
                for step in steps:
                    stage=self.stage(step['name'])
                    if stage:events[stage]=step['id']
            checked={step['id'] for step in steps if 'inspection' in step}
            try:
                self.begin_inspections(steps,bundle,events)
                self.move('play',[point['ticks'] for point in plan],steps,{k:v for k,v in events.items() if v not in checked})
                while self.inspection_batch.pending:
                    state,resumed=self.check()
                    if not settled(state):raise RuntimeError('최종 검사 중 자세 유지 상태 변경')
                    if self.linear_active:self.poll_linear()
                    if resumed:self.inspection_batch.restart()
                    self.poll_inspections();time.sleep(.05)
            except (PreparationRestart,ResetRequested):
                raise
            except Exception as exc:
                self.emergency()
                self.inspection_records.append({'result':'FAIL','error':str(exc),'at':time.time()})
                atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                self.emit('INSPECTION_FAILED:'+str(exc));raise
            finally:
                if self.inspection_batch:self.inspection_batch.close();self.inspection_batch=None
                self.pause_camera()
        else:self.move('play', [point['ticks'] for point in plan], steps, episode.get('completion_events'))

    def finish_episode(self):
        if self.finished_return_step is not None:
            self.start_finished_return(defer_pause=False)
            if not self.linear_return_started:
                raise RuntimeError('완제품 팔레트 안착 확인 전 · 리니어 조립 위치 복귀를 시작하지 않았습니다.')
            self.emit('CHAIN_RETURN_WAIT:target_mm=100:target_position=ASSEMBLY')
            self.wait_linear()
            self.emit('CHAIN_RETURN_DONE:target_mm=100:position_measured=false')
        self.emit('EPISODE_DONE')
        if self.assembly_evidence:self.assembly_evidence.commit()
        self.emit('DONE')


    def _execute(self, episode, bundle, speed, target):
        from so101_teach.episode_inspection import startup_steps
        # Each new arm/episode adopts its own basis; pauses within it retain it.
        self.adopted_jigs=None;self.inspection_jigs={}
        self.prepare_run_inspections(episode,bundle,speed,target)
        steps=self.prepare_safe_start(episode,bundle,speed,target)
        self.execution_bundle=deepcopy(bundle);self.execution_steps=deepcopy(steps)
        self.resume_configuration=self.configuration_state()
        # Only jig adoption and startup checks wait for safe-pose arrival.
        while True:
            self.preparation_active=True
            plan=self.retry_visual(lambda:self.prepare_execution_plan(steps,bundle,target,startup_steps(episode)),
                                   validate=self.prepare_camera)
            self.report_preparation();self.preparation_active=False
            self.emit('EPISODE_STARTED')
            if self.on_work_started is not None:
                callback,self.on_work_started=self.on_work_started,None
                callback()
            try:
                self.play_plan(episode,steps,bundle,plan)
            except PreparationRestart:
                self.emit('PLAN_DISCARDED:PREPARATION_CHANGED')
                continue
            break
        self.finish_episode()

    def reset_to_safe(self):
        if self.assembly_evidence:self.assembly_evidence.clear_run()
        self.emit('RESET_STARTED')
        # RESET cannot stop this stage either. Wait before any arm return motion.
        if self.linear_active:
            self.emit('RESET_WAIT_LINEAR_COMPLETE')
            self.wait_linear()
        state, _ = self.check()
        if self.link:
            if self.safe_ticks is None: raise RuntimeError('저장된 안전 자세가 없습니다.')
            if torque_off(state):
                self.emit('RESET_TORQUE_ON_STARTED')
                self.link.rpc('command',{'action':'arm'})
                state=self.wait(settled,15)
                self.emit('RESET_TORQUE_ON_CONFIRMED')
            elif not settled(state):raise RuntimeError('안전 복귀 전 최신 정지 자세를 확인할 수 없습니다.')
            self.emit('SAFE_RETURN_STARTED')
            self.move('move', [self.safe_ticks])
            self.emit('SAFE_RETURN_DONE')
        self.emit('RESET_DONE')


class Status:
    def __init__(self, arm, command, ros, *, publish_latest=True,status_fd=None):
        self.arm, self.command = arm, command
        self.publish_latest = publish_latest
        self.path = ROOT / 'data' / f'episode-cli-{arm}.json'
        self.process = None
        self.ros_writer=None;self.ros_socket=None;self.ros_output=None
        self.run_id = uuid.uuid4().hex
        self.log = ROOT / 'data/episode-cli-runs' / (self.run_id + '.json')
        self.events = [];self.pending_final=None;self.published=False;self.managed=False
        from episode_timing import JobTiming
        self.timing=JobTiming();self.timing.begin('preparation')
        from episode_analytics import ExecutionAnalysis
        self.analysis=ExecutionAnalysis(self.timing.job_id,self.timing.started)
        self.analysis.register_run(self.run_id,arm);self.analysis_environment=None
        self.write_pool=None;self.write_future=None;self.write_count=0
        self.run_journal=None;self.final_committed=False;self.latest_predecessor=self.run_id
        self.publish_lock=threading.RLock();self.control=None;self.stdout_lost=False
        if ros and status_fd is not None:
            from status_transport import OrderedStatusWriter,StatusSocketClient,StatusOutput
            self.ros_socket=StatusSocketClient(status_fd)
            self.ros_output=StatusOutput(client=self.ros_socket);self.ros_writer=OrderedStatusWriter(self.ros_output.send)
        elif ros:
            self.process = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'integration/episode_status.py'), arm],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            if not select.select([self.process.stdout], [], [], 6)[0] or self.process.stdout.readline().strip() != 'READY':
                self.process.terminate()
                self.process.wait(timeout=2)
                raise RuntimeError('ROS 상태 발행 시작 실패. ROS 환경을 확인하거나 --no-ros를 사용하세요.')
            from status_transport import OrderedStatusWriter,StatusOutput
            os.set_blocking(self.process.stdin.fileno(),False)
            self.ros_output=StatusOutput(process=self.process);self.ros_writer=OrderedStatusWriter(self.ros_output.send)

    def configure_analysis(self,operation,*,request_id=None,execution_started=True):
        self.analysis.configure(operation,self.timing.job_id,self.timing.started,request_id=request_id,
            source='action' if request_id else 'cli',execution_started=execution_started)
        if self.control is not None:self.control.analysis=self.analysis

    def emit(self, suffix):
        if self.control is not None:
            if suffix=='RESET_DONE':self.control.complete('reset')
            elif suffix.startswith(('FAILED','REJECTED')):self.control.fail_pending(exclude=('estop',))
        if suffix in ('DONE','CHAIN_DONE','RESET_DONE'):
            self.pending_final=suffix;return
        if suffix.startswith(('FAILED','REJECTED')):self.pending_final=None
        self.publish(suffix)

    def flush_final(self):
        self.flush_records()
        suffix=self.pending_final;self.pending_final=None
        if suffix is None:return
        if self.managed:
            print('READY_TO_COMPLETE',flush=True)
            if not select.select([sys.stdin],[],[],10)[0] or sys.stdin.readline().strip()!='COMPLETE':
                raise RuntimeError('완료 알림 인계 확인 실패')
        self.publish(suffix)

    def _write(self,record,events):
        # Capture provenance in the existing writer, never in a motor-control loop.
        analysis=record.get('_analysis')
        record={k:v for k,v in record.items() if k!='_analysis'}
        if self.analysis_environment is None:
            from so101_teach.runtime_version import manifest
            runtime=manifest(ROOT,allow_missing=True)
            boot=Path('/proc/sys/kernel/random/boot_id')
            self.analysis_environment={'runtime_sha256':runtime['sha256'],'runtime_schema':runtime['schema'],
                                       'boot_id':boot.read_text().strip() if boot.exists() else None}
        payload={'schema_version':2,'record_kind':'execution','run_id':self.run_id,'job_id':record['job_id'],'events':events,
                 'analysis':{**analysis,'environment':self.analysis_environment} if analysis is not None else None}
        if 'timing_summary' in record:payload['timing_summary']=record['timing_summary']
        if analysis is not None and analysis.get('record_complete'):
            from episode_accounting import summarize
            payload['accounting']=summarize(analysis,record.get('timing_summary'))
        # Small runs keep their original format and avoid an extra fsync.
        # Longer runs append exact deltas; explicit barriers still export the
        # original complete JSON for existing analysis tools and final archives.
        if self.run_journal is None and len(events)<32:atomic_json(self.log,payload)
        else:
            if self.run_journal is None:
                from so101_teach.execution_records import RunJournal
                self.run_journal=RunJournal(self.log)
            self.run_journal.append(payload,atomic_json)
        # Latest status is a derived projection. Never expose DONE before its
        # authoritative run commit, or let a cache failure undo that commit.
        first=not self.published;self.published=True
        if self.publish_latest:
            try:
                with self.path.with_suffix('.status.lock').open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    current=read_json(self.path) if self.path.exists() else {}
                    if not isinstance(current,dict):raise ValueError('최신 상태 파일 형식 오류')
                    owner=current.get('run_id')
                    if first:self.latest_predecessor=owner
                    if owner in (self.run_id,self.latest_predecessor):
                        atomic_json(self.path,record);self.latest_predecessor=self.run_id
                    else:self.latest_predecessor=self.run_id
            except (OSError,ValueError) as exc:
                self._warn('최신 상태 저장 실패 · 원본 실행 기록 사용',exc)
        # A compact index, atomically replaced, makes a two-arm chain one row.
        # Write it after its referenced run snapshot is durable. A reader can
        # verify revision equality and fall back to grouping run files if stale.
        try:
            atomic_json(ROOT/'data/episode-job-index'/(record['job_id']+'.json'),{
                'schema_version':1,'record_kind':'job_index','job_id':record['job_id'],
                'revision':analysis.get('event_count',0) if analysis else 0,
                'source_run_id':self.run_id,'source_file':'episode-cli-runs/'+self.log.name,
                'outcome':analysis.get('outcome') if analysis else None,
                'record_complete':bool(analysis and analysis.get('record_complete')),
                **({'accounting':payload['accounting']} if 'accounting' in payload else {})})
        except OSError as exc:
            # Derived cache only: the durable run remains authoritative. Readers
            # validate its revision or rebuild from runs; never fail a completed job.
            try:print('작업 색인 저장 실패 · 원본 실행 기록 사용: '+str(exc),file=sys.stderr,flush=True)
            except OSError:pass


    @staticmethod
    def _warn(message,exc):
        try:print('EXECUTOR_WARNING:'+message+': '+' '.join(str(exc).splitlines()),file=sys.stderr,flush=True)
        except OSError:pass

    def flush_records(self,*,materialize=True):
        with self.publish_lock:
            if self.write_future is not None:self.write_future.result()
            if materialize and self.run_journal is not None:
                try:self.run_journal.materialize(atomic_json)
                except OSError as exc:
                    if not self.final_committed:raise
                    self._warn('실행 기록 내보내기 실패 · 확정 저널 사용',exc)

    def handoff(self,arm,command):
        """Keep the already discovered ROS publishers and whole-job clock."""
        self.flush_records()
        other=Status(arm,command,False)
        other.process,self.process=self.process,None
        other.ros_socket,self.ros_socket=self.ros_socket,None
        # Both pending events and the sender survive the arm transition. Slow
        # ROS acknowledgments must not insert a new wait before arm3 starts.
        other.ros_output,self.ros_output=self.ros_output,None
        other.ros_writer,self.ros_writer=self.ros_writer,None
        other.timing=self.timing;other.managed=self.managed;other.control=self.control
        other.analysis=self.analysis;other.analysis.register_run(other.run_id,arm)
        other.analysis_environment=self.analysis_environment
        self.close()
        return other

    def publish(self, suffix):
        with self.publish_lock:
            if self.write_future is not None and self.write_future.done():self.write_future.result()
            text = self.command + '_' + suffix
            at=time.time()
            record = {'run_id': self.run_id, 'arm': self.arm, 'status': text, 'at': at,
                      'at_iso':datetime.fromtimestamp(at).astimezone().isoformat(timespec='milliseconds'),
                      **self.timing.record(self.arm,suffix)}
            mono=time.monotonic()
            record['event_seq']=len(self.events)+1
            record['at_monotonic_s']=mono
            if self.control is not None:
                with self.control.lock:
                    self.analysis.control_requests(self.control.receipts,self.control.supervision_lost)
                if suffix in ('DONE','CHAIN_DONE','RESET_DONE') or suffix.startswith(('FAILED','REJECTED')):
                    self.analysis.preparation_profile(self.control.startup_timing.snapshot())
            record['event']=self.analysis.event(self.arm,self.run_id,suffix,at,max(0.,mono-self.timing.started))
            record['job_event_seq']=record['event']['job_event_seq'];record['event_id']=record['event']['event_id']
            self.events.append(record)
            # Keep FIFO order and bound the queue. Workers never touch controls,
            # motion, stdout or ROS. A failed write prevents a success notification.
            if self.write_pool is None:self.write_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='episode-log')
            previous=self.write_future
            events=list(self.events)
            write_record={**record,'_analysis':self.analysis.snapshot()}
            def write():
                if previous is not None:previous.result()
                self._write(write_record,events)
            self.write_future=self.write_pool.submit(write);self.write_count+=1
            critical=(suffix in ('DONE','CHAIN_DONE','RESET_DONE','BUILD_DONE','PAUSED','ACCEPTED')
                      or suffix.startswith(('FAILED','REJECTED')))
            if suffix in ('DONE','CHAIN_DONE','RESET_DONE'):
                self.flush_records(materialize=False)
                self.final_committed=True
            if critical or self.write_count%32==0:self.flush_records(materialize=critical)
            if not self.stdout_lost:
                try:
                    if sys.stdout.isatty():
                        print(f"[{record['at_iso']} +{record['elapsed_s']:.3f}s] {text}",flush=True)
                        if 'timing_summary' in record:
                            from episode_timing import summary_lines
                            for line in summary_lines(record['timing_summary']):print(line,flush=True)
                    else:print(text,flush=True)
                except BrokenPipeError:
                    # A dead Action parent must not unwind the HOLD/IPC loop.
                    self.stdout_lost=True
                    if self.control is not None:self.control.command('estop')
            if self.ros_writer:
                try:self.ros_writer.submit({'arm':self.arm,'status':text})
                except Exception as exc:
                    if not self.final_committed:raise
                    self._warn('완료 후 ROS 상태 전달 실패 · 확정 결과 유지',exc)

    def close(self):
        try:self.flush_records()
        finally:
            if self.write_pool is not None:
                self.write_pool.shutdown(wait=True);self.write_pool=None
            self.close_publisher()

    def close_publisher(self):
        error=None
        if self.ros_writer:
            try:self.ros_writer.flush()
            except Exception as exc:error=exc
            finally:
                if self.ros_socket:self.ros_socket.close();self.ros_socket=None
                self.ros_writer.close();self.ros_writer=None
        if self.process:
            process,self.process=self.process,None
            try:
                try:process.stdin.close()
                except BrokenPipeError:pass
                try:process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill();process.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired) as exc:
                if error is None:error=exc
        self.ros_output=None
        if error is not None:
            if not self.final_committed:raise error
            self._warn('완료 후 ROS 상태 전달 실패 · 확정 결과 유지',error)


def control_path():
    return ROOT / 'data/episode-cli.sock'


def control_server(control, done):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        control_path().unlink(missing_ok=True)
        server.bind(str(control_path())); os.chmod(control_path(), 0o600)
        server.listen(8); server.settimeout(.2)
        while not done.is_set():
            try: conn, _ = server.accept()
            except socket.timeout: continue
            with conn:
                conn.settimeout(2)
                try:
                    raw=b''
                    while len(raw)<8192:
                        part=conn.recv(8192-len(raw))
                        if not part:break
                        raw+=part
                        if b'\n' in raw or raw in (b'inspect',b'estop',b'restart',b'reset') or raw.startswith(b'clear_error ') and len(raw)==44:break
                    command=raw.decode().strip()
                    if command in ('restart','reset'):raise ValueError('STOP_GENERATION_REQUIRED')
                    reply=control.command(command)
                except Exception as exc: reply = 'REJECTED:' + str(exc)
                try: conn.sendall((reply + '\n').encode())
                except OSError: pass
    control_path().unlink(missing_ok=True)


def send_control(command):
    if command in ('restart','reset'):
        from action_protocol import control_request
        try:control_request(ROOT,command)
        except ValueError as exc:return 'REJECTED:'+str(exc)
        return command.upper()+'_REQUESTED'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5); client.connect(str(control_path()))
        client.sendall(command.encode()); response = client.recv(1024).decode().strip()
    return response


@dataclass(frozen=True)
class JobPlan:
    arm: str
    letter: str
    target: float
    chained: bool = False
    pc_compute: bool = False
    slide_only: bool = False


def job_plans():
    """Process differences are data; saved recipes still own every motion/check."""
    plans={}
    for letter in ('a','b'):
        product=letter.upper()
        plans['build_'+letter]=JobPlan('arm2',product,100.)
        plans['load_'+letter]=JobPlan('arm3',product,1.5)
        plans['build_load_'+letter]=JobPlan('arm2',product,100.,chained=True)
        plans['pc_build_load_'+letter]=JobPlan('arm2',product,100.,chained=True,pc_compute=True)
    plans['slide_build']=JobPlan('arm2','SLIDE',100.,slide_only=True)
    plans['slide_load']=JobPlan('arm3','SLIDE',1.5,slide_only=True)
    return plans


JOB_PLANS=job_plans()


def recovery_context():
    """Only the operations needed by recovery; resolved at the call boundary."""
    return SimpleNamespace(ROOT=ROOT,Control=Control,Link=Link,Runner=Runner,Status=Status,
        ResetRequested=ResetRequested,ShutdownRequested=ShutdownRequested,
        activation_paused=activation_paused,at_safe_pose=at_safe_pose,settled=settled,
        stopped=stopped,torque_off=torque_off,control_path=control_path,
        control_server=control_server,finish_controls=finish_controls,
        load_job=load_job,open_links=open_links,preparation_context=preparation_context)


def preparation_context():
    return SimpleNamespace(ROOT=ROOT,Runner=Runner,recovery_context=recovery_context,
                           chain_start_records=chain_start_records,load_job=load_job)


def parse_command(argv):
    parser = argparse.ArgumentParser(description='조립대 직접 실행·비상정지·재시작')
    commands = ('build_a', 'build_b', 'load_a', 'load_b', 'build_load_a', 'build_load_b', 'pc_build_load_a', 'pc_build_load_b', 'slide_build', 'slide_load', 'prepare_home',
                'estop', 'restart', 'reset', 'build_status', 'load_status', 'slide_status')
    parser.add_argument('command', nargs='?', choices=commands)
    for arm in ('arm2', 'arm3'):
        for letter in ('a', 'b'):
            parser.add_argument(f'--{arm}-{letter}', dest='legacy', action='store_const', const=('build_' if arm == 'arm2' else 'load_') + letter, help=argparse.SUPPRESS)
    parser.add_argument('--check', action='store_true', help='파일만 검증; 장치 연결·이동 없음')
    parser.add_argument('--no-ros', action='store_true')
    parser.add_argument('--execution-lock-fd',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--managed-completion',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--recovery-arm',choices=('arm2','arm3'),help='전체 reset 결과를 기록할 요청 팔 (두 팔 모두 복귀)')
    parser.add_argument('--supervision-job-id',help=argparse.SUPPRESS)
    parser.add_argument('--supervision-client-id',help=argparse.SUPPRESS)
    parser.add_argument('--stop-fd',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--status-fd',type=int,help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    command = args.command or args.legacy
    if not command: parser.error('명령어가 필요합니다.')
    args.command=command
    return args


def run_home_command(command,args):
    import episode_recovery
    if args.check:
        episode_recovery.workcell_records(recovery_context())
        from standby_preparation import WorkcellPreparation
        WorkcellPreparation.records(SimpleNamespace(slide_only=True,control=Control()),preparation_context())
        print('CHECK_OK prepare_home · 저장 자세·양팔 준비 설정 검증 · 장치 연결 없음');return 0
    return episode_recovery.run(recovery_context(),'arm2',ros=not args.no_ros,
                                startup=True,execution_lock_fd=args.execution_lock_fd,
                                managed=args.managed_completion,supervision_job_id=args.supervision_job_id,stop_fd=args.stop_fd,status_fd=args.status_fd,supervision_client_id=args.supervision_client_id)


def run_control_command(command,args):
    if args.check:
        print('CHECK_OK '+command+' control request · 장치 연결 없음');return 0
    if command=='reset' and args.supervision_job_id is not None:
        # A reserved idle RESET must acquire the workcell lock itself. Do
        # not redirect it to a different job that appeared after inspection.
        import episode_recovery
        return episode_recovery.run(recovery_context(),args.recovery_arm,ros=not args.no_ros,
            supervision_job_id=args.supervision_job_id,stop_fd=args.stop_fd)
    from action_protocol import record_notice
    def control_notice(message):
        try:record_notice(ROOT,'workcell','CONTROL_'+command.upper()+':'+message)
        except Exception as exc:print('제어 알림 저장 실패: '+str(exc),file=sys.stderr)
    control_notice('REQUESTED')
    try:
        response = send_control(command); print(response)
        control_notice(response)
        return 1 if response.startswith('REJECTED:') else 0
    except OSError:
        if command in ('estop','reset'):
            from standby_control import cancel
            cancelled=cancel(ROOT)
            if command=='estop' and cancelled:
                print('PAUSED:STANDBY_CANCELLED');return 0
        if command=='reset':
            import episode_recovery
            return episode_recovery.run(recovery_context(),args.recovery_arm,ros=not args.no_ros,supervision_job_id=args.supervision_job_id,stop_fd=args.stop_fd)
        control_notice('REJECTED:NO_ACTIVE_JOB')
        print('REJECTED:NO_ACTIVE_JOB', file=sys.stderr); return 2


def show_command_status(command,args):
    from linear_client import LinearClient
    if command == 'slide_status':
        try: print(json.dumps(LinearClient().call('status'), ensure_ascii=False)); return 0
        except Exception as exc: print(str(exc), file=sys.stderr); return 1
    arm = 'arm2' if command == 'build_status' else 'arm3'
    path = ROOT / 'data' / f'episode-cli-{arm}.json'
    print(json.dumps(read_json(path) if path.exists() else {'status': 'NO_RUN'}, ensure_ascii=False)); return 0


class JobExecution:
    """Own one job's resources across both phases and all exit paths.

    The job lock and both links outlive a build/load handoff. Current phase
    fields change together; cleanup always observes the current owner.
    """
    def __init__(self,command,args):
        self.command,self.args=command,args
        self.plan=JOB_PLANS[self.command]
        self.arm,self.letter,self.target=self.plan.arm,self.plan.letter,self.plan.target
        self.pc_command,self.chained,self.slide_only=self.plan.pc_compute,self.plan.chained,self.plan.slide_only
        self.link = self.peer = self.linear = self.status = self.runner = self.lock = self.thread = self.next_inspector = None
        self.done, self.control = threading.Event(), Control()
        self.job_started=time.monotonic();self.execution_started=False
        self.compute=None;self.compute_guard=None
        self.preparation=None;self.standby_records=None
        self.stop_thread=self.control.configure_supervision(self.args.supervision_job_id,self.args.stop_fd,self.done)

    def run(self):
        try:
            self.reserve()
            self.load_recipes()
            if self.args.check:return self.check_only()
            self.connect()
            self.prepare_runner()
            self.prepare_chain()
            if self.standby_records is not None:
                from standby_preparation import WorkcellPreparation
                self.preparation=WorkcellPreparation(self,preparation_context())
                self.preparation.begin();self.preparation.prepare()
            self.execute_phases()
            self.schedule_release()
            return 0
        except ResetRequested:
            return self.reset_requested()
        except BaseException as exc:
            return self.failed(exc)
        finally:
            self.close()

    def reserve(self):
        if not self.args.check:
            if self.pc_command:
                from so101_teach.pc_compute import session_for
                self.compute=session_for(self.args.supervision_job_id,self.command);self.compute.stop=self.control.shutdown
                from so101_teach.pc_compute import start_guard
                def compute_lost(reason):
                    self.control.compute_error=reason;self.control.shutdown.set()
                    if self.control.emergency:self.control.emergency()
                self.compute_guard=start_guard(self.compute,self.done,compute_lost)
            path=ROOT/'data/episode-cli.lock'
            if self.args.execution_lock_fd is not None:
                if Path(f'/proc/self/fd/{self.args.execution_lock_fd}').resolve()!=path.resolve():raise ValueError('실행 잠금 인계 오류')
                self.lock=os.fdopen(self.args.execution_lock_fd,'a')
            else:self.lock=open(path,'a')
            try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('BUSY: 다른 작업이 실행 중입니다.') from None
            self.execution_started=True

    def load_recipes(self):
        if not self.slide_only: self.data, self.episode, self.bundle, self.speed, self.timeout = self.control.startup_timing.call(self.arm+'.load.total',load_job,self.arm,self.letter,timing=self.control.startup_timing)
        else: self.timeout = 60
        self.next_job=self.control.startup_timing.call('arm3.load.total',load_job,'arm3',self.letter,timing=self.control.startup_timing) if self.chained else None
        if self.pc_command:
            from so101_teach.pc_compute import validate_profile
            validate_profile(self.bundle['profile']);validate_profile(self.next_job[2]['profile'])
        from standby_preparation import WorkcellPreparation
        self.standby_records=WorkcellPreparation.records(self,preparation_context())
        if self.slide_only:self.timeout=180
        self.return_step=finished_return_step(self.next_job[1]) if self.chained else None
        self.start_records=chain_start_records({'arm2':(self.data,self.episode,self.bundle,self.speed,self.timeout),'arm3':self.next_job}) if self.chained else None

    def check_only(self):
        if self.chained:
            print(f'CHECK_OK {self.command} build={len(self.episode["steps"])} load={len(self.next_job[1]["steps"])} steps');return 0
        print(f'CHECK_OK {self.command} target_mm={self.target:g}' + ('' if self.slide_only else f" {self.episode['name']} {len(self.episode['steps'])} steps")); return 0

    def connect(self):
        from linear_client import LinearClient
        self.thread = threading.Thread(target=control_server, args=(self.control, self.done), daemon=True); self.thread.start()
        signal.signal(signal.SIGINT, lambda *_: self.control.command('estop'))
        signal.signal(signal.SIGTERM, lambda *_: self.control.shutdown.set())
        # Reserve both arm controllers and the shared linear stage for one job.
        self.link, self.peer = Link(self.arm), Link('arm3' if self.arm == 'arm2' else 'arm2')
        self.link.compute=self.peer.compute=self.compute
        self.link.preserve_idle_release=self.slide_only
        self.peer.preserve_idle_release=not self.chained
        self.link.timing=self.peer.timing=self.control.startup_timing
        self.status = Status(self.arm, self.letter, not self.args.no_ros,publish_latest=False,status_fd=self.args.status_fd);self.status.managed=self.args.managed_completion
        self.status.control=self.control;self.status.timing.job_id=self.control.job_id
        self.status.timing.begin(self.command,started=self.job_started)
        self.status.configure_analysis(self.command,request_id=self.args.supervision_job_id)
        if not self.slide_only:
            self.status.analysis.context(self.arm,self.episode,self.bundle,self.speed,self.target,self.timeout)
            if self.next_job:self.status.analysis.context('arm3',self.next_job[1],self.next_job[2],self.next_job[3],1.5,self.next_job[4])
        self.control.info = {'run_id': self.status.run_id, 'arm': self.arm, 'command': self.command}
        self.control.startup_timing.call('shared.open_links',open_links,self.link,self.peer)
        self.linear = LinearClient();self.control.startup_timing.call('shared.linear_open',self.linear.open)
        # A rejected connection must not replace another owner's last status.
        # The timer still starts at job entry and includes all connection work.
        self.status.publish_latest=True
        if self.chained:self.status.emit('CHAIN_STARTED:'+self.command)
        else:self.status.emit('PREPARATION_STARTED:'+self.command)

    def prepare_runner(self):
        if not self.slide_only:
            from episode_recovery import invalidate
            invalidate(ROOT,self.arm)
            from so101_teach.episode_inspection import has_inspections
            if not has_inspections(self.episode) or self.next_job and not has_inspections(self.next_job[1]):
                from so101_teach.assembly_evidence import invalidate as clear_assembly
                clear_assembly(ROOT/'data/assembly-evidence.json')
        self.runner = Runner(self.link, self.status.emit, self.control, self.timeout, peer=self.peer, linear=self.linear)

    def prepare_chain(self):
        if self.chained:
            self.chain_start={'records':self.start_records,'links':{'arm2':self.link,'arm3':self.peer}}
            self.runner.chain_start=self.chain_start
            self.runner.on_work_started=self.prepare_load_model

    def prepare_load_model(self):
        from so101_teach.episode_inspection import has_inspections
        if has_inspections(self.next_job[1]):
            from so101_teach.episode_inspection_runtime import PreparedInspector
            if getattr(self.peer,'keep_prepared',False):
                from so101_teach.prepared_standby import RemotePreparedInspector
                self.next_inspector=RemotePreparedInspector(self.peer,self.next_job[2]['profile'])
            else:self.next_inspector=PreparedInspector(self.next_job[2]['profile'],timing=self.control.startup_timing,timing_prefix='arm3.inspection',**({'compute':self.compute} if self.compute is not None else {}))
            self.status.analysis.resource_prepared('arm3','inspection_model','creation_requested_for_later_phase')
            self.status.emit('LOAD_MODEL_PREPARING')

    def save_safe_pose(self,arm,episode,bundle):
        from episode_recovery import save_ready
        last=episode['steps'][-1]
        if last.get('safe_boundary')=='end' and not last.get('jig_id'):
            save_ready(ROOT,arm,self.letter,episode,bundle,self.status.run_id,last['ticks'])

    def execute_phases(self):
        if self.slide_only:
            self.status.emit('ACCEPTED')
            if self.preparation is None:self.runner.prepare_linear(self.target)
            self.status.emit('DONE')
        else:
            self.save_safe_pose(self.arm,self.episode,self.bundle)
            if self.chained:self.save_safe_pose('arm3',self.next_job[1],self.next_job[2])
            self.runner.execute(self.episode, self.bundle, self.speed, self.target)
            if self.chained:self.execute_load_phase()

    def execute_load_phase(self):
        from episode_recovery import invalidate
        # Keep both controllers and the workcell lock across the handoff.
        # A failure/reset exits before this point; ESTOP pauses here too.
        self.runner.check()
        self.status.pending_final=None;self.status.emit('BUILD_DONE')
        self.link,self.peer=self.peer,self.link;self.arm='arm3';self.target=1.5
        self.data,self.episode,self.bundle,self.speed,self.timeout=self.next_job
        invalidate(ROOT,self.arm)
        self.status=self.status.handoff(self.arm,self.letter)
        self.control.info={'run_id':self.status.run_id,'arm':self.arm,'command':self.command}
        self.runner=Runner(self.link,self.status.emit,self.control,self.timeout,peer=self.peer,linear=self.linear)
        self.runner.prepared_inspector=self.next_inspector;self.next_inspector=None
        self.runner.prepared_start=self.chain_start['receipts']['arm3']
        self.runner.finished_return_step=self.return_step
        self.status.emit('CHAIN_LOAD_STARTED:'+self.command)
        self.save_safe_pose(self.arm,self.episode,self.bundle)
        self.runner.execute(self.episode,self.bundle,self.speed,self.target)
        self.status.emit('CHAIN_DONE')

    def schedule_release(self):
        if self.preparation is not None:
            self.preparation.finish();return
        if not self.slide_only:
            for connection in ((self.link,self.peer) if self.chained else (self.link,)):
                connection.rpc('schedule_idle_release')
            self.status.emit('IDLE_RELEASE_SCHEDULED:seconds=3')

    def reset_requested(self):
        if self.preparation is not None:self.preparation.committed=False
        if self.status:self.status.analysis.cancel()
        if self.runner:
            self.runner.failure_context['phase']='reset';self.runner.failure_inspection=None
        try:
            from episode_recovery import workcell_records,reset_workcell
            records=workcell_records(recovery_context())
            reset_workcell(recovery_context(),records,{self.arm:self.link,('arm3' if self.arm=='arm2' else 'arm2'):self.peer},self.linear,self.control,self.status.emit,active=self.runner,first=self.arm)
            return 130
        except Exception as exc:
            self.control.begin_ending((self.link,self.peer))
            self.runner.emergency()
            self.runner.save_failure_images(exc)
            if not self.slide_only:self.save_failure(exc)
            self.status.analysis.error(exc,'reset')
            self.status.emit('FAILED:RESET:' + str(exc));return 1

    def failed(self,exc):
        if self.preparation is not None:self.preparation.committed=False
        if self.command=='slide_load':
            for connection in (self.link,self.peer):
                if connection and connection.lease:
                    connection.keep_prepared=False
                    try:connection.rpc('standby_cancel')
                    except Exception:pass
        if self.status:self.status.analysis.error(exc,getattr(self.runner,'failure_context',{}).get('phase') if self.runner else 'preparation')
        self.control.begin_ending((self.link,self.peer))
        if self.runner:
            self.runner.emergency()
            self.runner.save_failure_images(exc)
        if self.runner and self.status and not self.slide_only:self.save_failure(exc)
        if self.status:
            try: self.status.emit(('FAILED:' if self.status.publish_latest else 'REJECTED:') + str(exc))
            except Exception: pass
        else:self.report_rejected(exc)
        return 1

    def report_rejected(self,exc):
        # A rejected contender must appear in work_watch without replacing
        # the current status of a job that already owns the device lock.
        if not self.args.check:
            try:
                rejected=Status(self.arm,self.letter,False,publish_latest=False)
                rejected.timing.job_id=self.control.job_id;rejected.timing.begin(self.command,started=self.job_started)
                rejected.configure_analysis(self.command,request_id=self.args.supervision_job_id,execution_started=self.execution_started)
                rejected.analysis.error(exc,'preparation')
                try:rejected.emit('REJECTED:'+str(exc))
                finally:rejected.close()
            except Exception as log_error:
                print(self.letter+'_REJECTED:'+str(exc),file=sys.stderr)
                print('거절 이력 저장 실패: '+str(log_error),file=sys.stderr)
        else:print(self.letter+'_REJECTED:'+str(exc),file=sys.stderr)

    def save_failure(self,exc):
        try:
            from episode_recovery import save_failure
            save_failure(ROOT,self.arm,self.letter,self.episode,self.bundle,self.status.run_id,self.runner.safe_ticks,exc)
        except Exception as error:print('실패 복귀 정보 저장 실패: '+str(error),file=sys.stderr)

    def close(self):
        if self.preparation is not None and not self.preparation.committed:self.preparation.cancel()
        if self.next_inspector is not None:
            try:self.next_inspector.close()
            except Exception as exc:print('운반 검사 모델 정리 실패: '+str(exc),file=sys.stderr)
        try:finish_controls(self.control,(self.link,self.peer))
        except Exception as exc:print('제어 완료 기록 정리 실패: '+str(exc),file=sys.stderr)
        self.done.set()
        if self.compute_guard:self.compute_guard.join(2)
        self.close_compute()
        if self.stop_thread:self.stop_thread.join(1)
        with self.control.lock:self.control.emergency=None
        if self.thread: self.thread.join(3)
        if self.linear:
            try: self.linear.close()
            except Exception as exc: print('리니어 연결 정리 확인 실패: ' + str(exc), file=sys.stderr)
        self.close_links()
        self.control.emergency=None
        if self.lock:self.lock.close()
        if self.status:
            try:self.status.flush_final()
            finally:self.status.close()

    def close_compute(self):
        if self.compute is not None:
            try:
                atomic_json(ROOT/'data/diagnostics'/('pc-compute-'+self.control.job_id+'.json'),{'job_id':self.control.job_id,'command':self.command,'requests':self.compute.metrics,'error':getattr(self.control,'compute_error',None)})
            except Exception as exc:print('PC 연산 기록 저장 실패: '+str(exc),file=sys.stderr)
            self.compute.close()

    def close_links(self):
        for connection in (self.link, self.peer):
            if connection and connection.lease:
                if connection is self.link and not self.slide_only and not getattr(connection,'keep_prepared',False):
                    try:
                        connection.rpc('detect_freeze', {'enabled': False}); connection.rpc('camera_stop')
                    except Exception: pass
                try: connection.close()
                except Exception as exc: print('연결 정리 확인 실패: ' + str(exc), file=sys.stderr)


def run_job(command,args):
    return JobExecution(command,args).run()


def main(argv=None):
    args=parse_command(argv);command=args.command
    if command in JOB_PLANS:return run_job(command,args)
    if command=='prepare_home':return run_home_command(command,args)
    if command.endswith('_status'):return show_command_status(command,args)
    return run_control_command(command,args)


if __name__ == '__main__':
    sys.exit(main())
