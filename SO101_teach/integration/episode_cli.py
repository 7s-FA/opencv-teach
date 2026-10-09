"""Pi-local episode command; uses the existing device runtime without a PC UI."""
import argparse
from copy import deepcopy
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


INSPECTION_PROTOCOL = 6


class ResetRequested(Exception):
    pass


class ShutdownRequested(Exception):
    pass


def load_job(arm, command):
    from so101_teach.configuration import JigCatalog
    from so101_teach.vision import PoseLatch
    from so101_teach.arm_workspace import require_calibrated
    data = ROOT / ('data' if arm == 'arm2' else 'data/arm3-runtime')
    profile, cal, reference = load_profile(data)
    if profile.get('robot_id') != arm:
        raise ValueError('다른 팔의 설정입니다.')
    require_calibrated(profile)
    settings = read_json(ROOT / 'integration/execution-settings.json')[arm]
    reference.set_trims(settings['trim_ticks'])
    catalog = JigCatalog(data, profile=profile)
    latch = PoseLatch(settings['hold_seconds'], settings['acquisition_seconds'], settings['acquisition_attempts'])
    app = SimpleNamespace(profile=profile, data_dir=data, catalog=catalog, reference=reference,
                          pose_latch=latch, active_jig=next(iter(catalog.items)))
    recipe = read_json(ROOT / 'integration/recipes.json')['recipes'][arm][command]
    episode_id = recipe['episode_id']
    if len(episode_id) != 32 or any(c not in '0123456789abcdef' for c in episode_id):
        raise ValueError('에피소드 ID 오류')
    episode = EpisodeStore(data / 'episodes', cal, arm).load(data / 'episodes' / (episode_id + '.json'))
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
    bundle = configuration_bundle(app)
    bundle['mesh_hashes'] = {key: catalog.mesh(key)['sha256'] for key in catalog.items}
    return data, episode, bundle, settings['speed'], float(recipe['timeout_seconds'])


class Link:
    def __init__(self, arm):
        self.arm = arm
        self.lease = None
        self.stop = threading.Event()
        self.error = None

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
        result = subprocess.run([sys.executable, '-B', '-m', 'so101_teach.remote_server',
                                 '--ensure', '--arm', self.arm], cwd=ROOT, capture_output=True,
                                text=True, timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-700:])
        info = json.loads(result.stdout)
        self.base = f"http://127.0.0.1:{info['port']}"
        self.token = info['token']
        self.lease = self.post('/acquire')['lease']
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def heartbeat(self):
        while not self.stop.is_set():
            try:
                state = self.post('/state', {'lease': self.lease, 'alive': True}, timeout=1)
                if state['detached']:
                    raise RuntimeError('실행부 연결이 끊겼습니다.')
            except Exception as exc:
                self.error = str(exc)
                return
            self.stop.wait(.2)

    def rpc(self, method, args=None):
        if self.error:
            raise RuntimeError(self.error)
        return self.post('/rpc', {'lease': self.lease, 'id': uuid.uuid4().hex,
                                 'method': method, 'args': args or {}}, timeout=40)

    def state(self):
        if self.error:
            raise RuntimeError(self.error)
        return self.post('/state')['follower']

    def close(self):
        if self.lease:
            try:
                self.post('/detach', {'lease': self.lease})
            finally:
                self.stop.set()
                self.thread.join(2)


def settled(state):
    sample = (state or {}).get('latest') or {}
    telemetry = sample.get('telemetry', {})
    return bool(state and state['running'] and state['state'] == 'HOLD'
                and not state['program_active'] and not state['command_pending']
                and sample.get('calibration_matches') and 0 <= time.monotonic() - sample.get('monotonic', 0) < .5
                and len(telemetry) == 6 and all(h.get('moving') == 0 for h in telemetry.values()))


def torque_off(state):
    sample = (state or {}).get('latest') or {}
    telemetry = sample.get('telemetry', {})
    return bool(state and state['running'] and not state.get('error') and state['state'] == 'READ_ONLY'
                and not state['program_active'] and not state['command_pending']
                and 0 <= time.monotonic() - sample.get('monotonic', 0) < .5
                and len(telemetry) == 6 and all(h.get('torque') == 0 for h in telemetry.values()))


class Control:
    def __init__(self):
        self.pause = threading.Event()
        self.resume = threading.Event()
        self.reset = threading.Event()
        self.shutdown = threading.Event()
        self.paused = False
        self.emergency = None
        self.info = None
        self.lock = threading.RLock()

    def command(self, command):
        with self.lock:
            if command == 'inspect':
                return json.dumps({'info': self.info, 'paused': self.paused})
            if command == 'estop':
                self.resume.clear(); self.reset.clear(); self.pause.set()
                if self.emergency: self.emergency()
                return 'ESTOP_REQUESTED'
            if command in ('restart', 'reset'):
                if not self.paused: raise ValueError('NOT_PAUSED')
                if self.resume.is_set() or self.reset.is_set(): raise ValueError('CONTROL_PENDING')
                (self.resume if command == 'restart' else self.reset).set()
                return command.upper() + '_REQUESTED'
            raise ValueError('UNKNOWN_CONTROL')


class Runner:
    def __init__(self, link, emit, control, timeout, peer=None, linear=None):
        self.link, self.peer, self.linear = link, peer, linear
        self.emit, self.control = emit, control
        self.deadline = time.monotonic() + timeout
        self.safe_ticks = None
        self.linear_active = False
        self.linear_target = None
        self.linear_until = None
        self.inspection_batch = None
        self.assembly_evidence = None
        self.inspection_jigs = {}
        self.inspection_due = []
        self.inspection_started = set()
        self.follower_phase = 'active'
        self.control.emergency = self.emergency

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
        if self.control.shutdown.is_set(): raise ShutdownRequested('PROCESS_SHUTDOWN')
        for source in (self.peer, self.linear):
            if source and source.error: raise RuntimeError(source.error)
        state = self.link.state() if self.link else None
        if self.follower_phase=='before_connect' and (not state or not state.get('running')):
            return None
        if self.follower_phase=='connecting' and (not state or not state.get('running')):
            if state and state.get('error'):raise RuntimeError(state['error'])
            return None
        if state and (state.get('error') or not state['running']):
            raise RuntimeError(state.get('error') or '팔로워 연결 종료')
        return state

    def prepare_follower(self, bundle, speed):
        """A retained closed session is not the new connection's result."""
        self.follower_phase='before_connect'
        try:
            self.check()
            self.emit('FOLLOWER_CONNECTING')
            self.link.rpc('configure', {'bundle': bundle})
            self.check()
            started=time.monotonic()
            self.link.rpc('connect', {'speed': speed})
            self.follower_phase='connecting'
            def ready(state):
                sample=(state or {}).get('latest') or {}
                return bool(state and state.get('running') and state['state'] in ('READ_ONLY','HOLD')
                            and not state.get('release_pending',False) and sample.get('calibration_matches') and started<=sample.get('monotonic',0)
                            and 0<=time.monotonic()-sample['monotonic']<.5)
            state=self.wait(ready,10)
            self.link.rpc('speed', {'rate': speed})
            self.emit('FOLLOWER_READY')
            return state
        finally:
            self.follower_phase='active'

    def await_pause(self):
        began = time.monotonic()
        self.emit('ESTOP_REQUESTED')
        errors = self.emergency()
        if errors: raise RuntimeError('정지 요청 실패: ' + '; '.join(errors))
        stopped = None
        while True:
            stopped = self.state()
            arm_ready = self.link is None or stopped is None or settled(stopped) or torque_off(stopped)
            if arm_ready: break
            if time.monotonic() - began > 15: raise RuntimeError('정지 확인 시간 초과')
            time.sleep(.05)
        if self.linear_active and not self.poll_linear():
            self.emit('LINEAR_CONTINUES:ESTOP_APPLIES_TO_ARM_ONLY')
        with self.control.lock: self.control.paused = True
        self.emit('PAUSED')
        while True:
            state = self.state()
            if state and not (settled(state) or torque_off(state)):
                raise RuntimeError('비상정지 중 자세 유지 상태 변경')
            if self.linear_active: self.poll_linear()
            with self.control.lock:
                if self.control.reset.is_set():
                    self.control.reset.clear(); self.control.pause.clear(); self.control.paused = False
                    self.deadline = time.monotonic() + 120
                    raise ResetRequested('RESET')
                if self.control.resume.is_set():
                    self.control.resume.clear(); self.control.pause.clear(); self.control.paused = False
                    break
            time.sleep(.05)
        self.deadline += time.monotonic() - began
        self.emit('RESUMED')
        return stopped

    def check(self):
        paused = False
        if self.control.pause.is_set():
            self.await_pause(); paused = True
        state = self.state()
        if time.monotonic() >= self.deadline: raise RuntimeError('실행 제한시간 초과')
        return state, paused

    def wait(self, predicate, seconds=None):
        until = time.monotonic() + seconds if seconds else self.deadline
        while True:
            before = self.deadline
            state, _ = self.check()
            until += self.deadline - before
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
                self.emit(f"STEP_DONE:{progress_offset+reported}/{total_steps or len(steps)}:{steps[reported-1]['name']}")
                for stage, end in stage_ends.items():
                    if reported == end: self.emit('STAGE_DONE:' + stage)
            if self.inspection_batch:
                for after,step in self.inspection_due:
                    if count>=after and step['id'] not in self.inspection_started:
                        self.inspection_batch.start(step);self.inspection_started.add(step['id'])
        self.check()
        request = self.link.rpc('command', {'action': action, 'targets': original})['request_id']
        while True:
            if self.control.pause.is_set():
                stopped = self.await_pause()
                if self.inspection_batch:self.inspection_batch.restart()
                if stopped.get('completed_request_id') == request:
                    progress(len(original))
                    return self.state()
                if stopped.get('request_id') != request:
                    raise RuntimeError('재시작할 실행 요청이 변경되었습니다.')
                count = stopped.get('index')
                if type(count) is not int or not 0 <= count <= len(original) - offset:
                    raise RuntimeError('재시작할 스텝 번호를 확인하지 못했습니다.')
                offset += count; progress(offset)
                self.check()
                if offset == len(original): return self.state()
                request = self.link.rpc('command', {'action': action, 'targets': original[offset:]})['request_id']
            state = self.state()
            if time.monotonic() >= self.deadline: raise RuntimeError('실행 제한시간 초과')
            if state and state.get('request_id') == request:
                count = state.get('index', 0)
                if type(count) is int: progress(min(len(original), offset + count))
            if self.inspection_batch:self.poll_inspections()
            if state and state.get('completed_request_id') == request and settled(state):
                progress(len(original)); return state
            # Unexpected HOLD (driver protection) must not look like a user pause.
            if state and state.get('request_id') == request and settled(state) and not self.control.pause.is_set():
                raise RuntimeError('드라이버 보호 정지: 자동 재개하지 않습니다.')
            time.sleep(.05)

    def start_linear(self, target):
        self.check()
        self.emit(f'LINEAR_CHECK:target_mm={target:g}')
        value = self.linear.call('ensure', target_mm=target)
        self.linear_target = target
        self.linear_until = time.monotonic() + 12
        self.linear_active = value['phase'] != 'TIMED_COMPLETE'
        if not self.linear_active:
            self.emit(f'LINEAR_READY:target_mm={target:g}:position_measured=false')
        else:
            self.emit(f'LINEAR_STARTED:target_mm={target:g}')

    def poll_linear(self):
        if not self.linear_active:
            return True
        value = self.linear.call('status')
        if value['target_mm'] != self.linear_target:
            raise RuntimeError('리니어 목표가 변경되었습니다.')
        if value['phase'] == 'TIMED_COMPLETE':
            self.linear_active = False
            self.emit('LINEAR_DONE:TIME_BASED:position_measured=false')
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

    def prepare_work(self, steps, bundle, target, startup_checks=()):
        # The user confirmed these are fixed jigs, independent of the linear stage.
        self.start_linear(target)
        while True:
            current = self.measure([*steps,*startup_checks], bundle)
            self.inspection_jigs=deepcopy(current)
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
            self.emit('JIG_RECHECK_AFTER_RESUME')
        self.emit('WORK_READY:LINEAR_COMPLETE_AND_JIG_CONFIRMED')
        if startup_checks:self.emit('STARTUP_INSPECTION_PASSED')
        return current

    def measure(self, steps, bundle):
        from so101_teach.episode_inspection import inspection_jig_ids
        keys = {s['jig_id'] for s in steps if s.get('jig_id')} | inspection_jig_ids(steps)
        if not keys: return {}
        self.link.rpc('detect_freeze', {'enabled': False})
        def begin():
            self.link.rpc('detect_clear')
            self.emit('JIG_DETECTION_STARTED')
            return time.monotonic()
        started = begin()
        until = started + bundle['acquisition_seconds'] * bundle['acquisition_attempts']
        self.link.rpc('camera_start', {'processing_enabled': True, 'preview_fps': 10})
        after_at = after_preview_at = None
        detected = set()
        try:
            while True:
                _, resumed = self.check()
                if self.linear_active: self.poll_linear()
                if resumed:
                    started = begin(); detected.clear()
                    until = started + bundle['acquisition_seconds'] * bundle['acquisition_attempts']
                frame = self.link.post('/camera', {'after_at': after_at, 'after_preview_at': after_preview_at})
                after_at, after_preview_at = frame.get('at'), frame.get('preview_at')
                if frame.get('error'): raise RuntimeError(frame['error'])
                now = time.monotonic(); results = frame.get('detection', {}).get('by_jig', {}); current = {}
                if 0 <= now - frame.get('at', 0) < 3 and frame.get('at', 0) >= started:
                    live = frame.get('detection', {}).get('live_by_jig', results)
                    for key in keys:
                        if key not in detected and (live.get(key, {}).get('selected') or live.get(key, {}).get('candidates')):
                            self.emit('JIG_DETECTED:' + key); detected.add(key)
                        result = results.get(key, {}); metric = (result.get('selected') or {}).get('metric')
                        measured = result.get('pose_measured_at')
                        if (metric and measured is not None and started <= measured <= now
                                and now - measured < bundle['hold_seconds']
                                and result.get('acquisition_completed_attempts', 0) <= bundle['acquisition_attempts']):
                            current[key] = {'pose': [*metric['center_xy_mm'], metric['yaw_deg']],
                                'symmetry_deg': metric.get('symmetry_deg', 90), 'stl_sha256': bundle['mesh_hashes'][key],
                                **({'mesh_yaw_offset_deg': metric['mesh_yaw_offset_deg']} if metric.get('mesh_yaw_offset_deg') else {})}
                if keys == current.keys():
                    self.link.rpc('detect_freeze', {'enabled': True})
                    for key in sorted(keys):
                        self.emit('JIG_CONFIRMED:' + key)
                        x, y, yaw = current[key]['pose']
                        self.emit(f'JIG_POSE:{key}:x_mm={x:.2f}:y_mm={y:.2f}:yaw_deg={yaw:.2f}')
                    return current
                if now >= until: raise RuntimeError('지그 측정 실패: ' + ', '.join(sorted(keys - current.keys())))
                time.sleep(.1)
        finally:
            self.link.rpc('camera_stop')

    def inspect_checkpoint(self,step,bundle,*,startup=False):
        import base64
        import cv2
        import numpy as np
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_preview import load_placement
        from so101_teach.episode_inspection import InspectionDecision
        from so101_teach.episode_inspection_runtime import FrameInspection
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        placement=load_placement(ROOT/'data')
        if placement and self.linear_target is not None:
            placement['linear_stage']['startup_state']={'known':not self.linear_active,'commanded_mm':self.linear_target,'moving':self.linear_active}
        worker=None
        def begin():
            product=step.get('inspection_product',self.inspection_product)
            return FrameInspection(data,bundle['profile'],catalog,placement,step['inspection'],product,fixed_jigs=self.inspection_jigs),InspectionDecision(step['inspection'],product,time.monotonic(),evidence=self.assembly_evidence)
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
                frame=self.link.post('/camera',{'after_at':after_at,'after_preview_at':after_preview})
                if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
                image=frame.get('preview_image') or frame.get('image')
                at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
                now=time.monotonic()
                if image and type(at) in (int,float) and decision.started<=at<=now and now-at<1:
                    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
                    if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                    worker.submit(decoded,at)
                after_at,after_preview=frame.get('at'),frame.get('preview_at')
                if decision.observe(worker.result,now):
                    if decision.basis:step={**step,'inspection_basis':deepcopy(decision.basis)}
                    if self.assembly_evidence:self.assembly_evidence.record(step,decision.product)
                    self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
                    atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                    self.emit('INSPECTION_PASS:'+step['id']);return
                time.sleep(.05)
        except Exception as exc:
            self.emergency()
            self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'result':'FAIL','error':str(exc),'at':time.time()})
            try:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            finally:self.emit('INSPECTION_FAILED:'+step['id']+':'+str(exc))
            raise
        finally:
            if worker:worker.close()
            self.link.rpc('camera_stop')

    def inspect_startup_group(self,steps,bundle):
        import base64,cv2,numpy as np
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_preview import load_placement
        from so101_teach.episode_inspection import GroupInspectionDecision
        from so101_teach.episode_inspection_runtime import FrameInspections
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        placement=load_placement(ROOT/'data');catalog=JigCatalog(data,profile=bundle['profile'])
        worker=None;linear_target=None;counts={};was_resumed=False
        try:
            if any(s['inspection']['station']=='linear' for s in steps):
                state=self.linear.call('status') if self.linear else {}
                if state.get('phase')!='TIMED_COMPLETE' or type(state.get('target_mm')) not in (int,float):raise RuntimeError('시작 전 검사: 리니어 정지·명령 위치 확인 필요')
                if not placement:raise RuntimeError('시작 전 검사: 작업대 좌표 없음')
                linear_target=state['target_mm'];placement['linear_stage']['startup_state']={'known':True,'commanded_mm':linear_target,'moving':False}
            def begin():
                decision=GroupInspectionDecision(steps,time.monotonic(),evidence=self.assembly_evidence)
                return FrameInspections(data,bundle['profile'],catalog,placement,steps,fixed_jigs=self.inspection_jigs),decision
            worker,decision=begin()
            self.emit('INSPECTION_GROUP_STARTED:'+str(len(steps)))
            for step in steps:self.emit('INSPECTION_STARTED:'+step['id']+':'+step['name'])
            self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})
            after_at=after_preview=submitted=None
            while True:
                state,resumed=self.check()
                if not (settled(state) or torque_off(state)):raise RuntimeError('시작 전 검사 중 자세 유지 상태 변경')
                if linear_target is not None:
                    stage=self.linear.call('status')
                    if stage.get('phase')!='TIMED_COMPLETE' or stage.get('target_mm')!=linear_target:raise RuntimeError('시작 전 검사 중 리니어 상태 변경')
                if resumed:
                    was_resumed=True
                    worker.close();worker,decision=begin();after_at=after_preview=submitted=None;counts.clear()
                    self.emit('INSPECTION_GROUP_RESTARTED')
                frame=self.link.post('/camera',{'after_at':after_at,'after_preview_at':after_preview})
                if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
                image=frame.get('preview_image') or frame.get('image')
                at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
                now=time.monotonic()
                if image and type(at) in (int,float) and 0<=now-at<1 and at!=submitted:
                    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
                    if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                    worker.submit(decoded,at);submitted=at
                after_at,after_preview=frame.get('at'),frame.get('preview_at')
                passed=decision.observe(worker.result,now)
                for step in passed:
                    self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'product':step['inspection_product'],'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
                    self.emit('INSPECTION_PASS:'+step['id'])
                if passed:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                for key,d in decision.pending.items():
                    if counts.get(key)!=d.count:
                        counts[key]=d.count;self.emit('INSPECTION_COUNT:'+key+f":{d.count}/{d.check.get('minimum_observations',2)}")
                worker.retain(decision.pending)
                if not decision.pending:return was_resumed
                time.sleep(.05)
        except Exception as exc:
            self.emergency();self.inspection_records.append({'result':'FAIL','error':str(exc),'at':time.time()})
            try:atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            finally:self.emit('INSPECTION_FAILED:'+str(exc))
            raise
        finally:
            if worker:worker.close()
            self.link.rpc('camera_stop')

    def begin_inspections(self,steps,bundle,events):
        from so101_teach.episode_inspection import inspection_schedule
        from so101_teach.episode_inspection_runtime import InspectionBatch,FrameInspection
        from so101_teach.configuration import JigCatalog
        from so101_teach.workcell_preview import load_placement
        self.inspection_due=inspection_schedule(steps);self.inspection_started=set()
        data=ROOT/('data' if bundle['profile'].get('robot_id','arm2')=='arm2' else 'data/arm3-runtime')
        catalog=JigCatalog(data,profile=bundle['profile']);placement=load_placement(ROOT/'data')
        if placement and self.linear_target is not None:
            placement['linear_stage']['startup_state']={'known':not self.linear_active,'commanded_mm':self.linear_target,'moving':self.linear_active}
        def worker(step):
            self.emit('INSPECTION_STARTED:'+step['id']+':'+step['name'])
            return FrameInspection(data,bundle['profile'],catalog,placement,step['inspection'],self.inspection_product,fixed_jigs=self.inspection_jigs)
        def passed(step):
            self.inspection_records.append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
            atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
            self.emit('INSPECTION_PASS:'+step['id'])
            for stage,key in events.items():
                if key==step['id']:self.emit('STAGE_DONE:'+stage)
        self.inspection_batch=InspectionBatch(self.inspection_product,worker,passed,evidence=self.assembly_evidence)
        self.inspection_after_at=self.inspection_after_preview=None
        self.link.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})

    def poll_inspections(self):
        if not self.inspection_batch or not self.inspection_batch.pending:return
        import base64,cv2,numpy as np
        # Read the already captured preview; the native classifier runs in its worker.
        frame=self.link.post('/camera',{'after_at':self.inspection_after_at,'after_preview_at':self.inspection_after_preview})
        if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
        image=frame.get('preview_image') or frame.get('image')
        at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
        view=None;now=time.monotonic()
        if image and type(at) in (int,float) and 0<=now-at<1:
            decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
            if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
            view=(decoded,at)
        self.inspection_after_at,self.inspection_after_preview=frame.get('at'),frame.get('preview_at')
        self.inspection_batch.poll(view)

    def execute(self, episode, bundle, speed, target):
        from so101_teach.episode_inspection import validate_inspection,has_inspections
        validate_inspection(episode)
        from so101_teach.episode_inspection import inspection_schedule,startup_steps
        inspection_schedule(episode['steps'])
        if has_inspections(episode):
            from so101_teach.assembly_evidence import AssemblyEvidence,geometry_scope
            from so101_teach.workcell_preview import load_placement
            self.assembly_evidence=AssemblyEvidence(ROOT/'data/assembly-evidence.json',scope=geometry_scope(load_placement(ROOT/'data')),
                episode_id=episode['id'])
            from so101_teach.episode_inspection_runtime import require_resources
            require_resources()
            self.inspection_product=episode['product_type'];self.inspection_episode=episode['id'];self.inspection_records=[]
            self.inspection_log=ROOT/'data/diagnostics'/f'episode-inspection-{time.time_ns()}.json'
            atomic_json(self.inspection_log,{'episode_id':episode['id'],'product_type':self.inspection_product,'checks':[]})
        last = episode['steps'][-1]
        self.safe_ticks = deepcopy(last['ticks']) if last.get('safe_boundary') == 'end' and not last.get('jig_id') else None
        state = self.prepare_follower(bundle,speed)
        self.emit('ACCEPTED')
        steps = deepcopy(episode['steps'])
        current = self.prepare_work(steps, bundle, target, startup_steps(episode))
        state, _ = self.check()
        if state['state'] == 'READ_ONLY': self.link.rpc('command', {'action': 'arm'})
        self.wait(settled, 15)
        if steps[0].get('safe_boundary') == 'start':
            self.emit('SAFE_POSE_STARTED')
            self.move('move', [steps.pop(0)['ticks']])
            self.emit('SAFE_POSE_REACHED')
        self.emit('PLAN_STARTED')
        plan = self.link.rpc('plan', {'steps': steps, 'current': current})['plan']
        self.emit('PLAN_READY')
        self.check(); self.emit('EPISODE_STARTED')
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
                    if resumed:self.inspection_batch.restart()
                    self.poll_inspections();time.sleep(.05)
            except Exception as exc:
                self.emergency()
                self.inspection_records.append({'result':'FAIL','error':str(exc),'at':time.time()})
                atomic_json(self.inspection_log,{'episode_id':self.inspection_episode,'product_type':self.inspection_product,'checks':self.inspection_records})
                self.emit('INSPECTION_FAILED:'+str(exc));raise
            finally:
                if self.inspection_batch:self.inspection_batch.close();self.inspection_batch=None
                self.link.rpc('camera_stop')
        else:self.move('play', [point['ticks'] for point in plan], steps, episode.get('completion_events'))
        self.emit('EPISODE_DONE')
        if self.assembly_evidence:self.assembly_evidence.commit()
        self.emit('DONE')

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
    def __init__(self, arm, command, ros, *, publish_latest=True):
        self.arm, self.command = arm, command
        self.publish_latest = publish_latest
        self.path = ROOT / 'data' / f'episode-cli-{arm}.json'
        self.process = None
        self.run_id = uuid.uuid4().hex
        self.log = ROOT / 'data/episode-cli-runs' / (self.run_id + '.json')
        self.events = [];self.pending_final=None;self.published=False;self.managed=False
        if ros:
            self.process = subprocess.Popen(['/usr/bin/python3', str(ROOT / 'integration/episode_status.py'), arm],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            if not select.select([self.process.stdout], [], [], 6)[0] or self.process.stdout.readline().strip() != 'READY':
                self.process.terminate()
                self.process.wait(timeout=2)
                raise RuntimeError('ROS 상태 발행 시작 실패. ROS 환경을 확인하거나 --no-ros를 사용하세요.')

    def emit(self, suffix):
        if suffix in ('DONE','CHAIN_DONE','RESET_DONE'):
            self.pending_final=suffix;return
        if suffix.startswith(('FAILED','REJECTED')):self.pending_final=None
        self.publish(suffix)

    def flush_final(self):
        suffix=self.pending_final;self.pending_final=None
        if suffix is None:return
        if self.managed:
            print('READY_TO_COMPLETE',flush=True)
            if not select.select([sys.stdin],[],[],10)[0] or sys.stdin.readline().strip()!='COMPLETE':
                raise RuntimeError('완료 알림 인계 확인 실패')
        self.publish(suffix)

    def publish(self, suffix):
        text = self.command + '_' + suffix
        record = {'run_id': self.run_id, 'arm': self.arm, 'status': text, 'at': time.time()}
        with self.path.with_suffix('.status.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            current=read_json(self.path) if self.path.exists() else {}
            if self.publish_latest and (not self.published or current.get('run_id')==self.run_id):atomic_json(self.path, record)
        self.published=True
        self.events.append(record)
        atomic_json(self.log, {'run_id': self.run_id, 'events': self.events})
        print(text, flush=True)
        if self.process:
            try:
                self.process.stdin.write(text + '\n')
                self.process.stdin.flush()
            except BrokenPipeError:
                print('ROS 상태 발행 종료: 터미널·JSON 상태를 확인하세요.', file=sys.stderr)

    def close(self):
        if self.process:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)


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
                try: reply = control.command(conn.recv(64).decode().strip())
                except Exception as exc: reply = 'REJECTED:' + str(exc)
                try: conn.sendall((reply + '\n').encode())
                except OSError: pass
    control_path().unlink(missing_ok=True)


def send_control(command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5); client.connect(str(control_path()))
        client.sendall(command.encode()); response = client.recv(1024).decode().strip()
    return response


def main(argv=None):
    from linear_client import LinearClient
    parser = argparse.ArgumentParser(description='조립대 직접 실행·비상정지·재시작')
    commands = ('build_a', 'build_b', 'load_a', 'load_b', 'build_load_a', 'build_load_b', 'slide_build', 'slide_load',
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
    args = parser.parse_args(argv)
    command = args.command or args.legacy
    if not command: parser.error('명령어가 필요합니다.')
    if command in ('estop', 'restart', 'reset'):
        if args.check:
            print('CHECK_OK '+command+' control request · 장치 연결 없음');return 0
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
            if command=='reset':
                import episode_recovery
                return episode_recovery.run(SimpleNamespace(**globals()),args.recovery_arm,ros=not args.no_ros)
            control_notice('REJECTED:NO_ACTIVE_JOB')
            print('REJECTED:NO_ACTIVE_JOB', file=sys.stderr); return 2
    if command.endswith('_status'):
        if command == 'slide_status':
            try: print(json.dumps(LinearClient().call('status'), ensure_ascii=False)); return 0
            except Exception as exc: print(str(exc), file=sys.stderr); return 1
        arm = 'arm2' if command == 'build_status' else 'arm3'
        path = ROOT / 'data' / f'episode-cli-{arm}.json'
        print(json.dumps(read_json(path) if path.exists() else {'status': 'NO_RUN'}, ensure_ascii=False)); return 0
    chained = command.startswith('build_load_')
    slide_only = command.startswith('slide_')
    arm = 'arm2' if command.startswith('build') or command == 'slide_build' else 'arm3'
    target = 100. if arm == 'arm2' else 1.5
    letter = 'SLIDE' if slide_only else command[-1].upper()
    link = peer = linear = status = runner = lock = thread = None
    done, control = threading.Event(), Control()
    try:
        if not args.check:
            path=ROOT/'data/episode-cli.lock'
            if args.execution_lock_fd is not None:
                if Path(f'/proc/self/fd/{args.execution_lock_fd}').resolve()!=path.resolve():raise ValueError('실행 잠금 인계 오류')
                lock=os.fdopen(args.execution_lock_fd,'a')
            else:lock=open(path,'a')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('BUSY: 다른 작업이 실행 중입니다.') from None
        if not slide_only: data, episode, bundle, speed, timeout = load_job(arm, letter)
        else: timeout = 60
        next_job=load_job('arm3',letter) if chained else None
        if args.check:
            if chained:
                print(f'CHECK_OK {command} build={len(episode["steps"])} load={len(next_job[1]["steps"])} steps');return 0
            print(f'CHECK_OK {command} target_mm={target:g}' + ('' if slide_only else f" {episode['name']} {len(episode['steps'])} steps")); return 0
        thread = threading.Thread(target=control_server, args=(control, done), daemon=True); thread.start()
        signal.signal(signal.SIGINT, lambda *_: control.command('estop'))
        signal.signal(signal.SIGTERM, lambda *_: control.shutdown.set())
        if not slide_only:
            from episode_recovery import invalidate
            invalidate(ROOT,arm)
            from so101_teach.episode_inspection import has_inspections
            if not has_inspections(episode) or next_job and not has_inspections(next_job[1]):
                from so101_teach.assembly_evidence import invalidate as clear_assembly
                clear_assembly(ROOT/'data/assembly-evidence.json')
        # Reserve both arm controllers and the shared linear stage for one job.
        link, peer = Link(arm), Link('arm3' if arm == 'arm2' else 'arm2')
        link.open(); peer.open()
        linear = LinearClient(); linear.open()
        status = Status(arm, letter, not args.no_ros);status.managed=args.managed_completion
        control.info = {'run_id': status.run_id, 'arm': arm}
        if chained:status.emit('CHAIN_STARTED:'+command)
        runner = Runner(None if slide_only else link, status.emit, control, timeout, peer=peer, linear=linear)
        if slide_only:
            status.emit('ACCEPTED'); runner.prepare_linear(target); status.emit('DONE')
        else:
            from episode_recovery import save_ready
            last=episode['steps'][-1]
            if last.get('safe_boundary')=='end' and not last.get('jig_id'):
                save_ready(ROOT,arm,letter,episode,bundle,status.run_id,last['ticks'])
            runner.execute(episode, bundle, speed, target)
            if chained:
                # Keep both controllers and the workcell lock across the handoff.
                # A failure/reset exits before this point; ESTOP pauses here too.
                runner.check()
                status.pending_final=None;status.emit('BUILD_DONE');status.close();status=None
                link,peer=peer,link;arm='arm3';target=1.5
                data,episode,bundle,speed,timeout=next_job
                invalidate(ROOT,arm)
                status=Status(arm,letter,not args.no_ros);status.managed=args.managed_completion
                control.info={'run_id':status.run_id,'arm':arm}
                runner=Runner(link,status.emit,control,timeout,peer=peer,linear=linear)
                status.emit('CHAIN_LOAD_STARTED:'+command)
                last=episode['steps'][-1]
                if last.get('safe_boundary')=='end' and not last.get('jig_id'):
                    save_ready(ROOT,arm,letter,episode,bundle,status.run_id,last['ticks'])
                runner.execute(episode,bundle,speed,target)
                status.emit('CHAIN_DONE')
        if not slide_only:
            for connection in ((link,peer) if chained else (link,)):
                connection.rpc('schedule_idle_release')
            status.emit('IDLE_RELEASE_SCHEDULED:seconds=3')
        return 0
    except ResetRequested:
        try:
            from episode_recovery import workcell_records,reset_workcell
            records=workcell_records(SimpleNamespace(**globals()))
            reset_workcell(SimpleNamespace(**globals()),records,{arm:link,('arm3' if arm=='arm2' else 'arm2'):peer},linear,control,status.emit,active=runner,first=arm)
            return 130
        except Exception as exc:
            runner.emergency()
            if not slide_only:
                try:
                    from episode_recovery import save_failure
                    save_failure(ROOT,arm,letter,episode,bundle,status.run_id,runner.safe_ticks,exc)
                except Exception as error:print('실패 복귀 정보 저장 실패: '+str(error),file=sys.stderr)
            status.emit('FAILED:RESET:' + str(exc));return 1
    except BaseException as exc:
        if runner: runner.emergency()
        if runner and status and not slide_only:
            try:
                from episode_recovery import save_failure
                save_failure(ROOT,arm,letter,episode,bundle,status.run_id,runner.safe_ticks,exc)
            except Exception as error:print('실패 복귀 정보 저장 실패: '+str(error),file=sys.stderr)
        if status:
            try: status.emit('FAILED:' + str(exc))
            except Exception: pass
        else:
            # A rejected contender must appear in work_watch without replacing
            # the current status of a job that already owns the device lock.
            if not args.check:
                try:Status(arm,letter,False,publish_latest=False).emit('REJECTED:'+str(exc))
                except Exception as log_error:
                    print(letter+'_REJECTED:'+str(exc),file=sys.stderr)
                    print('거절 이력 저장 실패: '+str(log_error),file=sys.stderr)
            else:print(letter+'_REJECTED:'+str(exc),file=sys.stderr)
        return 1
    finally:
        done.set()
        with control.lock:control.emergency=None
        if thread: thread.join(3)
        if linear:
            try: linear.close()
            except Exception as exc: print('리니어 연결 정리 확인 실패: ' + str(exc), file=sys.stderr)
        for connection in (link, peer):
            if connection and connection.lease:
                if connection is link and not slide_only:
                    try:
                        connection.rpc('detect_freeze', {'enabled': False}); connection.rpc('camera_stop')
                    except Exception: pass
                try: connection.close()
                except Exception as exc: print('연결 정리 확인 실패: ' + str(exc), file=sys.stderr)
        control.emergency=None
        if lock:lock.close()
        if status:
            try:status.flush_final()
            finally:status.close()


if __name__ == '__main__':
    sys.exit(main())
