"""Loopback-only Pi runtime. SSH carries the authenticated transport.

Motor loops, leader conversion, vision and IK run here. A lost PC heartbeat
pauses motion through the existing MotionSession watchdog; no automatic resume.
"""
import argparse,base64,fcntl,hashlib,hmac,json,math,os,queue,secrets,subprocess,sys,tempfile,threading,time,uuid
from collections import OrderedDict,deque
from copy import deepcopy
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .domain import ROOT,JOINTS,Snapshot,Calibration,EpisodeStore,load_profile,atomic_json,read_json
from .remote_config import PROTOCOL,code_version,materialize,bundle_digest

class LeaderBridge:
    """Latest PC leader sample, expressed on the Pi monotonic clock."""
    def __init__(self,calibration):
        self.calibration=calibration;self.running=True;self.latest=None;self.error=None;self.events=queue.Queue();self.last_sequence=-1
    def close(self):self.running=False;self.latest=None
    def join(self,timeout=1):return True

class Runtime:
    def __init__(self,data_dir,*,motion_factory=None,camera_factory=None,robot_id=None):
        self.robot_id=robot_id
        from .motion import MotionSession
        from .devices import CameraSession
        self.data_dir=Path(data_dir);self.motion_factory=motion_factory or MotionSession;self.camera_factory=camera_factory or CameraSession
        self.camera_encode_lock=threading.Lock();self.camera_encoded=OrderedDict()
        from .idle_release import IdleRelease
        self.idle_release=IdleRelease();self.handoff_ready=False
        self.lock=threading.RLock();self.control_lock=threading.RLock();self.session=None;self.leader=None;self.camera=None;self.worker=None
        self.profile=None;self.cal=None;self.ref=None;self.kin=None;self.catalog=None;self.detector=None
        self.lease=None;self.last_heartbeat=0.;self.detached=False;self.events=deque(maxlen=256);self.event_id=0
        self.requests=OrderedDict();self.generation=0;self.bundle_hash=None;self.stop=threading.Event();self.version=code_version()
        self.instance=uuid.uuid4().hex;self.plans=OrderedDict();self.watch=threading.Thread(target=self.watchdog,daemon=True);self.watch.start()
    def event(self,role,kind,value):
        self.event_id+=1;self.events.append({'id':self.event_id,'role':role,'kind':kind,'value':value})
    def busy(self):return bool(self.session and self.session.running and (self.session.state in ('ACTIVATING','MOVING','FOLLOW') or self.session.command_pending.is_set() or self.session.program_active.is_set()))
    def require_lease(self,lease):
        if not self.lease or not hmac.compare_digest(str(lease),self.lease):raise ValueError('원격 제어 연결이 바뀌었습니다. 다시 연결하세요.')
        if self.detached or time.monotonic()-self.last_heartbeat>2:raise ValueError('PC 연결이 끊겨 동작을 정지했습니다. Pi 연결을 다시 열어 주세요.')
    def watchdog(self):
        while not self.stop.wait(.03):
            self.poll_idle_release()
            # Drain event queues without waiting behind an IK/file request.
            for role,worker in (('follower',self.session),('leader',self.leader),('calibration',self.worker)):
                if not worker:continue
                while True:
                    try:kind,value=worker.events.get_nowait()
                    except queue.Empty:break
                    if kind=='sample':continue
                    if role=='calibration' and kind=='saved':value=self.calibration_result(value)
                    self.event(role,kind,value)
            if self.lease and not self.detached and time.monotonic()-self.last_heartbeat>2:
                self.detached=True
                if self.session and self.session.running:
                    self.session.hold_requested.set()
                if self.worker and self.worker.running:self.worker.close()
                if self.camera:self.camera.close()
                self.event('remote','error','PC 연결 지연 · 이동 정지·자세 유지 · 자동 재개하지 않습니다.')
    def poll_idle_release(self):
        if self.idle_release.session is None or not self.lock.acquire(blocking=False):return
        try:
            shared=self.data_dir.parent if self.data_dir.name=='arm3-runtime' else self.data_dir
            with (shared/'episode-cli.lock').open('a') as work:
                try:fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    self.idle_release.since=None;self.idle_release.baseline=None;return
                if self.idle_release.poll(self.session,time.monotonic()):
                    self.event('follower','notice','새 명령 없음 · 3초 정지 확인 후 토크 해제 요청')
                    from integration.action_protocol import record_notice
                    record_notice(shared.parent,self.robot_id or 'workcell','IDLE_TORQUE_RELEASE_REQUESTED')
        except Exception as exc:
            self.idle_release.cancel();self.event('follower','error','자동 토크 해제 확인 실패: '+str(exc))
        finally:self.lock.release()

    def calibration_result(self,path):
        p=Path(path);side=p.with_suffix('.angles.json')
        return {'json':base64.b64encode(p.read_bytes()).decode(),'angles':base64.b64encode(side.read_bytes()).decode() if side.exists() else None}
    def heartbeat(self,lease,alive):
        if lease==self.lease and not self.detached and alive is True:
            self.last_heartbeat=time.monotonic()
            if self.session:self.session.heartbeat=self.last_heartbeat
    def detach(self,lease):
        if lease!=self.lease:raise ValueError('종료할 Pi 연결이 다릅니다.')
        self.detached=True
        if self.busy():self.session.hold_requested.set()
        if self.camera:self.camera.close()
        if self.worker and self.worker.running:self.worker.close()
        return {}
    def acquire(self):
        with self.lock,self.control_lock:
            if self.lease and not self.detached and time.monotonic()-self.last_heartbeat<2 and not self.handoff_ready:raise ValueError('다른 PC 창이 Pi를 사용 중입니다.')
            if self.busy():raise ValueError('Pi에서 동작 정리 중입니다. 잠시 후 다시 연결하세요.')
            if self.worker and self.worker.running:raise ValueError('Pi 보정 정리 중입니다.')
            self.idle_release.cancel();self.handoff_ready=False
            self.lease=uuid.uuid4().hex;self.last_heartbeat=time.monotonic();self.detached=False;self.requests.clear()
            if self.leader:self.leader.latest=None;self.leader.last_sequence=-1
            return {'lease':self.lease,'instance':self.instance,'event_id':self.event_id}
    def configure(self,bundle):
        if self.robot_id is not None and bundle['profile'].get('robot_id','arm2')!=self.robot_id:
            raise ValueError('다른 로봇팔의 설정·명령을 이 연결에 적용할 수 없습니다.')
        if self.busy() or self.worker and self.worker.running:raise ValueError('Pi 실행·보정이 끝난 뒤 설정을 적용하세요.')
        from .vision import StableCandidate
        from .jig_consensus import checked_seconds,checked_attempts,HOLD_RANGE
        acquisition_seconds=StableCandidate(bundle.get('acquisition_seconds',3.)).seconds
        attempts_limit=checked_attempts(bundle.get('acquisition_attempts',3))
        checked_seconds(bundle['hold_seconds'],HOLD_RANGE,'실행 기준 유지 시간')
        digest=bundle_digest(bundle)
        if digest==self.bundle_hash:return {'generation':self.generation}
        with tempfile.TemporaryDirectory() as temp:
            materialize(temp,bundle);profile,cal,ref=load_profile(temp)
            from .configuration import JigCatalog,model_tcp
            from .geometry import Kinematics
            cat=JigCatalog(temp,profile=profile)
            for key in cat.items:cat.mesh(key)
            ref.set_trims(bundle['trim_ticks']);Kinematics(ref,tcp=profile['tcp'])
        if self.session and self.session.running:
            if self.profile.get('robot_id','arm2')!=profile.get('robot_id','arm2'):raise ValueError('Pi 로봇 연결을 해제한 뒤 팔을 전환하세요.')
            old=(self.profile['port'],self.cal.sha256,self.cal.angle_mapping.sha256 if self.cal.angle_mapping else None,self.profile.get('leader'))
            new=(profile['port'],cal.sha256,cal.angle_mapping.sha256 if cal.angle_mapping else None,profile.get('leader'))
            if old!=new:raise ValueError('Pi 로봇 연결을 해제한 뒤 로봇·각도 보정을 변경하세요.')
            geometry_keys=('tcp','model_reference','table_z_mm','world_from_base','intrinsics','extrinsics')
            if any(self.profile.get(key)!=profile.get(key) for key in geometry_keys) or self.ref.trims!=ref.trims:
                sample=self.session.latest
                if (self.session.state!='READ_ONLY' or sample is None or not sample.fresh()
                        or len(sample.telemetry)!=6 or any(h.get('torque')!=0 for h in sample.telemetry.values())):
                    raise ValueError('Pi 팔로워의 토크 OFF를 확인한 뒤 좌표·TCP·모델 보정을 변경하세요.')
        camera_running=bool(self.camera and self.camera.running)
        camera_mode={'processing_enabled':getattr(self.camera,'processing_enabled',True),'preview_fps':getattr(self.camera,'preview_fps',10)}
        reuse_camera=bool(camera_running and self.profile['camera']==profile['camera'] and hasattr(self.camera,'set_processor'))
        self.camera_reconfiguring=camera_running
        try:
            if not reuse_camera:
                if self.camera:self.camera.close();self.camera.join(3)
                if self.camera and self.camera.running:raise ValueError('Pi 카메라 연결 정리 중')
                self.camera=None
            materialize(self.data_dir,bundle);self.profile,self.cal,self.ref=load_profile(self.data_dir);self.ref.set_trims(bundle['trim_ticks'])
            self.kin=Kinematics(self.ref,tcp=self.profile['tcp']);self.catalog=JigCatalog(self.data_dir,profile=self.profile,persist_migration=True)
            from .vision_service import MultiDetector
            self.detector=MultiDetector(self.catalog,self.profile,active=bundle['active_jig']);self.detector.seconds=float(bundle['hold_seconds']);self.detector.acquisition_seconds=acquisition_seconds;self.detector.attempts_limit=attempts_limit;self.detector.refresh()
            for latch in self.detector.latches.values():latch.configure(self.detector.seconds,acquisition_seconds=acquisition_seconds,attempts_limit=attempts_limit)
            self.store=EpisodeStore(self.data_dir/'episodes',self.cal,profile.get('robot_id','arm2'))
            self.generation+=1;self.bundle_hash=digest;self.plans.clear()
            if reuse_camera:self.camera.set_processor(self.detector.process)
            elif camera_running:self.start_camera(**camera_mode)
            return {'generation':self.generation,'calibration_sha256':self.cal.sha256}
        finally:self.camera_reconfiguring=False
    def snapshot(self,s):
        if not s:return None
        return {'role':s.role,'ticks':s.ticks,'telemetry':s.telemetry,'monotonic':s.monotonic,'wall_time':s.wall_time,'calibration_sha256':s.calibration_sha256,'calibration_matches':s.calibration_matches,'port':s.port}
    def state(self,lease,alive=False,after=0):
        self.heartbeat(lease,alive)
        s=self.session
        return {'server_now':time.monotonic(),'instance':self.instance,'generation':self.generation,'detached':self.detached,
                'events':[deepcopy(e) for e in list(self.events) if e['id']>after],
                'follower':None if not s else {'running':s.running,'error':s.error,'state':s.state,'latest':self.snapshot(s.latest),'index':s.index,'request_id':s.active_request_id,'completed_request_id':s.completed_request_id,'program_active':s.program_active.is_set(),'command_pending':s.command_pending.is_set(),'release_pending':s.release_requested.is_set(),'gripper_hold_tick':s.grip_contact.hold_tick},
                'leader':None if not self.leader else {'running':self.leader.running,'error':self.leader.error,'latest':self.snapshot(self.leader.latest)},
                'calibration':None if not self.worker else {'running':self.worker.running,'state':self.worker.state}}
    def camera_frame(self,after_at=None,after_preview_at=None):
        import cv2
        changing_before=getattr(self,'camera_reconfiguring',False);generation=self.generation
        c=self.camera;obs=c.observation if c and getattr(c,'processing_enabled',True) else None;now=time.monotonic()
        result={'server_now':now,'generation':generation,'running':bool(c and c.running),'error':c.error if c else None}
        def encode(frame,at):
            # Repeated HTTP readers share one JPEG for each captured frame.
            key=(id(c),at)
            with self.camera_encode_lock:
                if key not in self.camera_encoded:
                    ok,encoded=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,82])
                    self.camera_encoded[key]=base64.b64encode(encoded).decode() if ok else None
                    while len(self.camera_encoded)>4:self.camera_encoded.popitem(last=False)
                return self.camera_encoded[key]
        changing=changing_before or getattr(self,'camera_reconfiguring',False) or generation!=self.generation
        if changing:result.update(running=True,error=None)
        # Transfer within the same finite window used for new measurements.
        # Keep the original capture timestamp; preview age remains independent.
        from .camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
        if obs and 0<=now-obs[2]<MEASUREMENT_MAX_AGE_SECONDS and not changing:
            frame,detection,at=obs;result.update(detection=detection,at=at)
            if at!=after_at:result['image']=encode(frame,at)
        raw_preview=getattr(c,'preview_frame',None) if c else None
        preview=(raw_preview[0],None,raw_preview[1]) if raw_preview else getattr(c,'preview_observation',None) if c else None
        if preview and now-preview[2]<1:
            frame,_,at=preview;result['preview_at']=at
            if at!=after_preview_at and (changing or not obs or at!=obs[2]):result['preview_image']=encode(frame,at)
        return result
    def start_camera(self,**mode):
        if not self.profile:raise ValueError('Pi 설정을 먼저 적용하세요.')
        if self.camera and self.camera.running and getattr(self.camera,'stop',None) and self.camera.stop.is_set():
            self.camera.join(1.)
            if self.camera.running:raise ValueError('Pi 카메라 해제 중입니다. 잠시 후 다시 연결하세요.')
        if not self.camera or not self.camera.running:
            self.camera=self.camera_factory(**self.profile['camera'],processor=self.detector.process);self.camera.max_fps=10
            if mode:self.camera.set_mode(**mode)
            self.camera.start()
        elif mode:self.camera.set_mode(**mode)
        return {}
    def call(self,method,args):
        if method=='handoff_shutdown':
            job=args.get('job_id')
            if not isinstance(job,str) or len(job)!=32 or any(c not in '0123456789abcdef' for c in job):raise ValueError('종료 작업 ID 오류')
            with self.control_lock:
                if self.worker and self.worker.running:raise ValueError('보정 중에는 종료 작업으로 인계할 수 없습니다.')
                if not self.session or not self.session.running:raise ValueError('연결된 팔로워 없음')
                self.idle_release.cancel();self.handoff_ready=False
                self.lease=uuid.uuid4().hex;self.last_heartbeat=time.monotonic();self.detached=False
                self.session.heartbeat=self.last_heartbeat
                if self.leader:self.leader.close()
                self.event('remote','notice','Pi 안전 종료 작업에 제어권 인계: '+job)
                return {'lease':self.lease,'instance':self.instance}
        # A borrowed GUI execution lease must be validated on every read too.
        # This RPC never refreshes the UI heartbeat or acquires ownership.
        if method=='claim_episode':
            self.idle_release.cancel();self.handoff_ready=False;return {}
        if method=='schedule_idle_release':
            self.idle_release.schedule(self.session);self.handoff_ready=True;return {'seconds':3}
        if method=='execution_state':return self.state(self.lease)['follower']
        if method=='configure':return self.configure(args['bundle'])
        if not self.profile:raise ValueError('Pi 설정을 먼저 적용하세요.')
        if method=='camera_start':return self.start_camera(**args)
        if method=='camera_mode':
            if self.camera:self.camera.set_mode(**args)
            return {}
        if method=='camera_stop':
            if self.camera:self.camera.close()
            return {}
        if method=='detect_clear':
            if not self.detector.clear(args.get('jig')):raise ValueError('실행 중 지그 기준 고정')
            if self.camera:self.camera.observation=None
            self.generation+=1;return {'generation':self.generation}
        if method=='detect_freeze':
            self.detector.freeze(bool(args['enabled']));return {}
        if method=='connect':
            from .arm_workspace import require_calibrated
            require_calibrated(self.profile)
            if self.worker and self.worker.running:raise ValueError('보정이 진행 중입니다.')
            if not self.session or not self.session.running:
                self.session=self.motion_factory(self.profile['port'],self.cal,rate_ticks_s=args['speed'],grip_contact_load=self.profile.get('grip_contact_load',80),follow_start_rate_ticks_s=self.profile.get('follow_start_rate_ticks_s',400.),audit_path=self.data_dir/'diagnostics'/f'pi-session-{time.time_ns()}.json')
                self.session.heartbeat=time.monotonic();self.session.start()
            if self.profile.get('mode')=='leader':self.start_leader()
            return {}
        if method=='leader_start':self.start_leader();return {}
        if method=='disconnect':
            for s in (self.session,self.leader):
                if s:s.close()
            return {}
        if method=='speed':self.session.set_speed(args['rate']);return {}
        if method=='command':
            self.idle_release.cancel();self.handoff_ready=False
            if self.detached:raise ValueError('원격 연결을 다시 열어 주세요.')
            if args.get('action') not in ('hold','release'):
                from .arm_workspace import require_calibrated
                require_calibrated(self.profile)
            if args.get('action') in ('move','play','follow'):
                from .assembly_evidence import invalidate_manual_motion
                shared=self.data_dir.parent if self.data_dir.name=='arm3-runtime' else self.data_dir
                invalidate_manual_motion(shared)
            return {'request_id':self.session.request(args['action'],args.get('targets'))}
        if method=='save_episode':
            return {'id':self.store.load(self.store.save(args['episode']))['id']}
        if method=='delete_episode':
            key=args['id']
            if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('에피소드 ID 오류')
            self.store.delete(key);return {}
        if method=='plan':
            from .geometry import corrected_plan
            steps=args['steps'];current=args.get('current') or {}
            # Validate step ticks and references through the same episode validator.
            ep=self.store.new('원격 계산');ep['steps']=deepcopy(steps)
            # This RPC computes geometry for a step subset, not an episode.
            # Validate check fields without requiring absent episode metadata;
            # full episode validation remains at save/load and execution.
            from .episode_inspection import validate_check
            for step in ep['steps']:
                if 'inspection' in step:
                    validate_check(step['inspection'])
                    if step.get('safe_boundary'):raise ValueError('안전 자세에는 안착 검사를 지정할 수 없습니다.')
                    step.pop('inspection')
                step.pop('safe_boundary',None)
            self.store.validate(ep)
            plans=corrected_plan(self.kin,steps,current,lambda key:self.catalog.mesh(key)['sha256'])
            return {'plan':plans,'calibration_sha256':self.cal.sha256,'bundle_hash':self.bundle_hash}
        if method=='cal_start':return self.start_calibration(args)
        if method=='cal_command':
            if not self.worker or not self.worker.running:raise ValueError('Pi 보정을 먼저 시작하세요.')
            value=args['command'];self.worker.commands.put(tuple(value) if isinstance(value,list) else value);return {}
        if method=='cal_stop':
            if self.worker:self.worker.close()
            return {}
        raise ValueError('지원하지 않는 Pi 명령')
    def start_leader(self):
        config=self.profile.get('leader') or {}
        if not config.get('calibration_file'):raise ValueError('PC 리더의 보정 파일을 등록하세요.')
        if not self.session:raise ValueError('팔로워를 먼저 연결하세요.')
        cal=Calibration(self.data_dir/config['calibration_file'])
        if not self.leader or not self.leader.running:self.leader=LeaderBridge(cal)
        self.session.leader_calibration=cal;self.session.leader_provider=lambda:self.leader.latest if self.leader.running else None
    def leader_sample(self,body):
        self.require_lease(body.get('lease'))
        bridge=self.leader
        if not bridge or not bridge.running:return {'accepted':False}
        seq=body.get('sequence')
        if type(seq) is not int or seq<=bridge.last_sequence:return {'accepted':False}
        bridge.last_sequence=seq;data=body.get('sample')
        if not data:bridge.latest=None;return {'accepted':True}
        at=data.get('pi_monotonic');now=time.monotonic()
        if type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=.5:
            return {'accepted':False,'reason':'stale'}
        if data.get('calibration_sha256')!=bridge.calibration.sha256 or data.get('calibration_matches') is not True:
            bridge.latest=Snapshot('leader',{}, {},now,data.get('wall_time',time.time()),bridge.calibration.sha256,False,data.get('port','PC'))
            return {'accepted':False,'reason':'calibration'}
        if bridge.latest and at<=bridge.latest.monotonic:return {'accepted':False,'reason':'out_of_order'}
        ticks=bridge.calibration.ticks(data['ticks'],within_limits=False)
        bridge.latest=Snapshot('leader',ticks,{},at,data['wall_time'],bridge.calibration.sha256,True,data.get('port','PC'))
        return {'accepted':True}
    def start_calibration(self,args):
        from .calibration import CalibrationWorker
        if any(s and s.running for s in (self.session,self.leader,self.worker)):raise ValueError('Pi 로봇 연결을 해제한 뒤 보정하세요.')
        role=args['role']
        if role!='follower':raise ValueError('리더 보정은 리더가 연결된 PC에서 수행합니다.')
        from .arm_workspace import progress_path
        if args['port']!=self.profile['port']:raise ValueError('선택한 로봇팔의 등록 포트에서 보정하세요.')
        config=self.profile;cal=Calibration(self.data_dir/config['calibration_file'])
        target=self.data_dir/'calibration'/f'{role}-{time.time_ns()}.json';progress=progress_path(self.data_dir,role,self.profile)
        preset=None
        if args.get('preset'):
            preset=self.data_dir/'calibration'/f'import-{time.time_ns()}.json';preset.write_bytes(base64.b64decode(args['preset']['json'],validate=True))
            if args['preset'].get('angles'):preset.with_suffix('.angles.json').write_bytes(base64.b64decode(args['preset']['angles'],validate=True))
            imported=Calibration(preset)
            if self.profile.get('calibration_status')=='pending' and not imported.angle_mapping:
                raise ValueError('초기 연결용 파일은 모터 보정으로 적용할 수 없습니다. 새 3점 보정을 진행하세요.')
        resume=read_json(progress) if not preset and progress.exists() else None
        self.worker=CalibrationWorker(args['port'],cal,target,preset=preset,progress_path=progress,resume=resume);self.worker.start();return {}
    def rpc(self,body):
        if body.get('method')=='command' and body.get('args',{}).get('action') in ('hold','release'):
            with self.control_lock:
                self.require_lease(body.get('lease'))
                try:return {'ok':True,'value':self.call('command',body['args'])}
                except Exception as exc:return {'ok':False,'error':str(exc)}
        with self.lock:
            self.require_lease(body.get('lease'))
            key=body.get('id');method=body.get('method');args=body.get('args',{})
            if not isinstance(key,str) or len(key)>80:raise ValueError('명령 ID 오류')
            signature=hashlib.sha256(json.dumps([method,args],sort_keys=True).encode()).hexdigest()
            if key in self.requests:
                old,value=self.requests[key]
                if old!=signature:raise ValueError('같은 ID의 다른 명령은 허용하지 않습니다.')
                return value
            try:value={'ok':True,'value':self.call(method,args)}
            except Exception as exc:value={'ok':False,'error':str(exc)}
            self.requests[key]=(signature,value)
            while len(self.requests)>256:self.requests.popitem(last=False)
            return value
    def close(self):
        self.stop.set()
        for s in (self.worker,self.camera,self.leader,self.session):
            if s:s.close()
        for s in (self.camera,self.leader,self.session):
            if s:s.join(3)

def inventory():
    from .camera_inventory import camera_inventory
    details=camera_inventory()
    return {'serial':[str(p) for p in Path('/dev/serial/by-id').glob('*')],
            'cameras':[item['source'] for item in details],'camera_details':details}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):self.send_error(403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=30*1024*1024:raise ValueError('요청 크기 오류')
            body=json.loads(self.rfile.read(size),parse_constant=lambda _:(_ for _ in ()).throw(ValueError('잘못된 숫자')))
            runtime=self.server.runtime
            if self.path=='/health':value={'ok':True,'value':{'protocol':PROTOCOL,'version':runtime.version,'instance':runtime.instance,'devices':inventory()}}
            elif self.path=='/acquire':value={'ok':True,'value':runtime.acquire()}
            elif self.path=='/detach':value={'ok':True,'value':runtime.detach(body.get('lease'))}
            elif self.path=='/state':value={'ok':True,'value':runtime.state(body.get('lease'),body.get('alive'),int(body.get('after',0)))}
            elif self.path=='/leader':value={'ok':True,'value':runtime.leader_sample(body)}
            elif self.path=='/camera':value={'ok':True,'value':runtime.camera_frame(body.get('after_at'),body.get('after_preview_at'))}
            elif self.path=='/rpc':value=runtime.rpc(body)
            else:raise ValueError('잘못된 요청')
        except Exception as exc:value={'ok':False,'error':str(exc)}
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        try:self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):pass

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def get_request(self):
        sock,address=super().get_request();sock.settimeout(10);return sock,address

def runtime_directory(arm):
    if arm not in ('arm2','arm3'):raise ValueError('지원하지 않는 로봇팔 구분입니다.')
    return ROOT/'data' if arm=='arm2' else ROOT/'data/arm3-runtime'

def serve(port,arm='arm2'):
    data_dir=runtime_directory(arm);data_dir.mkdir(parents=True,exist_ok=True)
    lock=open(data_dir/'remote-server.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    token_path=data_dir/'remote-token'
    if not token_path.exists():
        fd=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    token_path.chmod(0o600);runtime=Runtime(data_dir,robot_id=arm)
    server=Server(('127.0.0.1',port),Handler);server.token=token_path.read_text().strip();server.runtime=runtime
    atomic_json(data_dir/'remote-server.json',{'pid':os.getpid(),'port':port,'instance':runtime.instance,'protocol':PROTOCOL,'version':runtime.version,'robot_id':arm})
    try:server.serve_forever(poll_interval=.2)
    finally:runtime.close();server.server_close()

def stop_stale_idle_server(current,port,arm='arm2'):
    """Only an unattached, device-free old runtime may be replaced on reopen."""
    import urllib.request,signal
    def read(endpoint):
        req=urllib.request.Request(f'http://127.0.0.1:{port}/'+endpoint,data=b'{}',headers={'Authorization':'Bearer '+current['token']})
        with urllib.request.urlopen(req,timeout=.5) as response:payload=json.load(response)
        if not payload.get('ok'):raise RuntimeError('Pi 실행부 상태를 확인하지 못했습니다.')
        return payload['value']
    for _ in range(30):
        state=read('state');camera=read('camera')
        if (state.get('instance')!=current.get('instance') or camera.get('running')
                or any((state.get(key) or {}).get('running') for key in ('follower','leader','calibration'))):
            raise RuntimeError('Pi 프로그램 갱신 대기: 카메라·팔로워·리더 연결을 해제하고 기존 앱을 종료한 뒤 다시 연결하세요.')
        if state.get('detached'):break
        time.sleep(.1)
    else:raise RuntimeError('Pi 프로그램 갱신 대기: 기존 PC 앱을 종료한 뒤 다시 연결하세요.')
    info=read_json(runtime_directory(arm)/'remote-server.json');pid=info['pid']
    if info.get('instance')!=current['instance'] or info.get('port')!=port or info.get('version')!=current['version']:
        raise RuntimeError('Pi 실행부 식별 정보가 변경되어 갱신을 중단했습니다.')
    process=Path(f'/proc/{pid}')
    if b'so101_teach.remote_server' not in (process/'cmdline').read_bytes().split(b'\0') or (process/'cwd').resolve()!=ROOT.resolve():
        raise RuntimeError('Pi 실행부 프로세스를 확인하지 못했습니다.')
    os.kill(pid,signal.SIGTERM)

def ensure(port,arm='arm2'):
    import urllib.request
    data_dir=runtime_directory(arm);data_dir.mkdir(parents=True,exist_ok=True);token_path=data_dir/'remote-token'
    def ready():
        if not token_path.exists():return None
        token=token_path.read_text().strip()
        req=urllib.request.Request(f'http://127.0.0.1:{port}/health',data=b'{}',headers={'Authorization':'Bearer '+token})
        try:
            with urllib.request.urlopen(req,timeout=.5) as f:result=json.load(f)
            if result['ok']:return {**result['value'],'token':token,'port':port}
        except (OSError,ValueError,KeyError):return None
    current=ready()
    if current and current.get('version')!=code_version():
        stop_stale_idle_server(current,port,arm)
        for _ in range(30):
            current=ready()
            if not current:break
            time.sleep(.1)
        else:raise RuntimeError('이전 Pi 실행부가 종료되지 않았습니다.')
    if not current:
        log=open(data_dir/'remote-server.log','ab')
        subprocess.Popen([sys.executable,'-B','-m','so101_teach.remote_server','--serve','--port',str(port),'--arm',arm],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        for _ in range(60):
            time.sleep(.1);current=ready()
            if current:break
    if not current:raise RuntimeError('Pi 실행부 시작 실패: data/remote-server.log 확인')
    print(json.dumps(current))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--serve',action='store_true');p.add_argument('--ensure',action='store_true');p.add_argument('--port',type=int);p.add_argument('--arm',choices=('arm2','arm3'),default='arm2');a=p.parse_args()
    port=a.port if a.port is not None else (8765 if a.arm=='arm2' else 8766)
    if a.serve:serve(port,a.arm)
    elif a.ensure:ensure(port,a.arm)
    else:p.error('--serve 또는 --ensure 필요')
