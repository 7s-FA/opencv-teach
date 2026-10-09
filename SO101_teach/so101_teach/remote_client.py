"""PC adapters for the Pi runtime. No remote mode fallback to local USB."""
import base64,json,queue,shlex,socket,subprocess,threading,time,uuid
from copy import deepcopy
from collections import deque
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from .domain import ROOT,Snapshot,atomic_json,read_json
from .motion import MotionSession
from .devices import ReadOnlySession,CameraSession
from .vision_service import MultiDetector
from .remote_config import PROTOCOL,code_version,configuration_bundle,bundle_digest

STATE_POLL_SECONDS=1/30  # Match the display budget; serial/control timing is unchanged.

def server_version_supported(version,protocol,*,current=None,compatibility=None):
    """Exact reviewed client/server pair permits a PC-only rolling update."""
    current=current or code_version()
    if protocol!=PROTOCOL:return False
    if version==current:return True
    try:pair=read_json(compatibility or ROOT/'assets/remote-client-compatibility.json')
    except (OSError,ValueError):return False
    return (isinstance(pair,dict) and pair.get('schema')==1
            and pair.get('client_code_version')==current
            and pair.get('server_code_version')==version)


def ssh_args(config):
    args=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5','-o','ServerAliveInterval=2','-o','ServerAliveCountMax=2','-o','ForwardAgent=no','-o','ForwardX11=no','-p',str(config['port'])]
    if config.get('identity_file'):args+=['-o','IdentitiesOnly=yes','-i',config['identity_file']]
    return args

def snapshot(data,offset):
    if not data:return None
    return Snapshot(data['role'],data['ticks'],data['telemetry'],data['monotonic']+offset,data['wall_time'],data['calibration_sha256'],data['calibration_matches'],data['port'])

def translate_detection(value,offset):
    if isinstance(value,dict):return {k:(v+offset if k in ('pose_measured_at','acquisition_started_at','acquisition_completed_at') and isinstance(v,(int,float)) else translate_detection(v,offset)) for k,v in value.items()}
    if isinstance(value,list):return [translate_detection(v,offset) for v in value]
    return value

class RemoteLink:
    def __init__(self,config,*,robot_id='arm2'):
        if robot_id not in ('arm2','arm3'):raise ValueError('지원하지 않는 로봇팔 구분입니다.')
        self.robot_id=robot_id
        from .pi_connection import validate
        self.leader_client=None;self.leader_thread=None;self.clock_anchor=None;self.leader_sequence=0;self.clock_samples=deque(maxlen=512)
        self.follower_requested=None;self.leader_upload_error=None;self.transport_events=deque(maxlen=100);self.audit_path=None
        self.config=validate(config,True);self.stop=threading.Event();self.tunnel=None;self.error=None;self.lease=None;self.listeners={};self.event_id=0;self.generation=0;self.bundle_hash=None;self.latest_state=None;self.ui_heartbeat=time.monotonic();self.heartbeat_provider=lambda:self.ui_heartbeat;self.state_thread=None;self.rpc_lock=threading.RLock()
    def open(self):
        c=self.config;folder=c['app_dir']
        # Shell expansion is used only for the leading home directory marker.
        folder='"$HOME"/'+shlex.quote(folder[2:]) if folder.startswith('~/') else shlex.quote(folder)
        command=f'cd {folder} && OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 {shlex.quote(c["python"])} -B -m so101_teach.remote_server --ensure --arm {shlex.quote(self.robot_id)}'
        start=subprocess.run(ssh_args(c)+['-l',c['user'],c['host'],command],capture_output=True,text=True,timeout=15)
        if start.returncode:raise ValueError('Pi 실행부 연결 실패: '+start.stderr.strip()[-600:])
        info=json.loads(start.stdout);self.token=info['token'];self.server_port=info['port']
        if not server_version_supported(info['version'],info['protocol']):raise ValueError('PC와 Pi 프로그램 버전이 다릅니다. Pi 실행부를 갱신하세요.')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        self.base=f'http://127.0.0.1:{port}'
        args=ssh_args(c)+['-o','ExitOnForwardFailure=yes','-N','-L',f'127.0.0.1:{port}:127.0.0.1:{info["port"]}','-l',c['user'],c['host']]
        self.tunnel=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            for _ in range(12):
                if self.tunnel.poll() is not None:raise ValueError('Pi 통신 통로를 열 수 없습니다.')
                try:self.health=self.http('/health',{},timeout=1.);break
                except OSError:time.sleep(.1)
            else:raise ValueError('Pi 응답 대기 시간 초과')
            if self.health['instance']!=info['instance']:raise ValueError('Pi 실행부가 변경되었습니다. 다시 연결하세요.')
            acquired=self.http('/acquire',{});self.lease=acquired['lease'];self.instance=acquired['instance'];self.event_id=acquired['event_id']
            self.ui_heartbeat=time.monotonic();self.state_thread=threading.Thread(target=self.poll_state,daemon=True);self.state_thread.start();self.leader_thread=threading.Thread(target=self.poll_leader,daemon=True);self.leader_thread.start()
            return self
        except BaseException:self.close(disconnect=False);raise
    def http(self,path,payload,timeout=1.5,*,base=None):
        req=Request((base or self.base)+path,data=json.dumps(payload,allow_nan=False).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        with urlopen(req,timeout=timeout) as response:data=json.load(response)
        if not data.get('ok'):raise ValueError(data.get('error','Pi 요청 실패'))
        return data['value']
    def rpc(self,method,args=None,timeout=3.):
        owner=getattr(self,'execution_owner',None)
        if owner and owner.busy and not (method=='command' and (args or {}).get('action') in ('hold','release') or method=='disconnect'):
            raise ValueError('Pi 에피소드 실행 중에는 설정·다른 동작을 변경할 수 없습니다.')
        if self.error:raise ConnectionError(self.error)
        body={'lease':self.lease,'id':uuid.uuid4().hex,'method':method,'args':args or {}}
        try:return self.http('/rpc',body,timeout)
        except OSError as exc:
            self.fail('Pi 통신 중단 · 명령 처리 여부 확인 필요: '+str(exc));raise ConnectionError(self.error) from exc
    def camera_rpc(self,method,args=None):
        owner=getattr(self,'execution_owner',None)
        if owner and owner.busy and not (method=='camera_stop' and owner.thread is None):
            raise ValueError('Pi 에피소드 실행부가 카메라를 사용 중입니다.')
        if method not in ('camera_start','camera_stop','camera_mode'):raise ValueError('카메라 제어 요청만 허용됩니다.')
        if self.error:raise ConnectionError(self.error)
        # Camera commands do not move motors. A video-only error must not close
        # the independently polled control session; never use this for motion.
        return self.http('/rpc',{'lease':self.lease,'id':uuid.uuid4().hex,'method':method,'args':args or {}},timeout=1.5)
    def sync(self,app):return self.sync_bundle(configuration_bundle(app))
    def sync_bundle(self,bundle):
        digest=bundle_digest(bundle)
        if digest!=self.bundle_hash:
            result=self.rpc('configure',{'bundle':bundle},timeout=8)
            self.bundle_hash=digest;self.generation=result['generation']
        return self.bundle_hash
    def record_transport(self,channel,message):
        self.transport_events.append({'at':time.time(),'channel':channel,'message':message})
        if self.audit_path:
            try:atomic_json(self.audit_path,{'target':self.config['host'],'events':list(self.transport_events)})
            except OSError:pass
    def leader_upload_status(self,error):
        if error==self.leader_upload_error:return
        self.leader_upload_error=error
        message=('리더 값 전송 지연 · 팔로워 연결 유지 · 최신 값으로 재시도: '+error) if error else '리더 값 전송 복구 · 짧은 수신 대기는 계속, 정지된 따라가기는 다시 시작하세요.'
        self.record_transport('leader',message)
        reader=self.leader_client
        if reader and hasattr(reader,'publish'):reader.publish('device_notice',message)
    def fail(self,message):
        if self.error:return
        self.error=message;self.stop.set();self.record_transport('connection',message)
        if self.leader_client:self.leader_client.close()
        for listener in list(self.listeners.values()):listener.failed(message)
    def update_clock_anchor(self,server_now,started,received):
        # Keep a low-latency receipt anchor: one slow status reply must not make
        # every subsequent leader sample artificially old. Still conservative:
        # use receive time rather than assuming symmetric network latency.
        self.clock_samples.append((server_now,started,received))
        while self.clock_samples and received-self.clock_samples[0][2]>30.:
            self.clock_samples.popleft()
        best=min(self.clock_samples,key=lambda sample:sample[2]-sample[1])
        self.clock_anchor=(best[0],best[2])
    def poll_state(self):
        last_ok=time.monotonic();delayed=False
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                state=self.http('/state',{'lease':self.lease,'alive':time.monotonic()-self.heartbeat_provider()<1.5,'after':self.event_id},timeout=1.)
                if self.error or self.stop.is_set():return
                if state['instance']!=self.instance:raise ValueError('Pi 실행부가 다시 시작되었습니다. 연결을 다시 열어 주세요.')
                last_ok=time.monotonic()
                if delayed:self.record_transport('state','Pi 상태 수신 복구');delayed=False
                self.latest_state=state;self.update_clock_anchor(state['server_now'],started,time.monotonic());offset=started-state['server_now']
                for role,listener in list(self.listeners.items()):listener.consume(state.get(role),offset,started)
                for event in state['events']:
                    self.event_id=max(self.event_id,event['id']);listener=self.listeners.get(event['role'])
                    if listener:listener.event(event['kind'],event['value'])
                if state['detached']:raise ValueError('Pi 연결 지연으로 이동이 정지됐습니다. 원격 연결을 다시 열어 주세요.')
            except OSError as exc:
                if self.stop.is_set():return
                # Read-only status can be retried. Motion RPCs remain single-shot.
                if isinstance(exc,HTTPError) and exc.code in (401,403):self.fail('Pi 인증 실패: '+str(exc));return
                if time.monotonic()-last_ok>=2.:
                    self.fail('Pi 상태 응답이 2초 이상 없습니다. 이동은 정지·유지되며 재접속이 필요합니다: '+str(exc));return
                if not delayed:self.record_transport('state','Pi 상태 수신 지연 · 연결 유지·재시도: '+str(exc));delayed=True
                self.stop.wait(.05);continue
            except Exception as exc:self.fail(str(exc));return
            self.stop.wait(max(.01,STATE_POLL_SECONDS-(time.monotonic()-started)))
    def poll_leader(self):
        last=None;cleared=True
        while not self.stop.is_set() and not self.error:
            began=time.monotonic()
            follower=(self.latest_state or {}).get('follower') or {}
            if follower.get('state') in ('MOVING','ACTIVATING'):
                # Episode targets already live on Pi; leader uploads are unrelated.
                self.stop.wait(.03);continue
            reader=self.leader_client;sample=reader.latest if reader and reader.running else None;anchor=self.clock_anchor
            data=None
            if sample and sample.fresh() and anchor:
                if (id(reader),sample.monotonic)==last:self.stop.wait(.005);continue
                # Receipt anchor makes the sample older by the response transit time,
                # never artificially fresh after a delayed network packet.
                data={'ticks':sample.ticks,'pi_monotonic':anchor[0]+sample.monotonic-anchor[1],
                      'wall_time':sample.wall_time,'calibration_sha256':sample.calibration_sha256,
                      'calibration_matches':sample.calibration_matches,'port':sample.port}
                last=(id(reader),sample.monotonic);cleared=False
            elif cleared:self.stop.wait(.02);continue
            else:cleared=True
            self.leader_sequence+=1
            try:
                result=self.http('/leader',{'lease':self.lease,'sequence':self.leader_sequence,'sample':data},timeout=.4)
                self.leader_upload_status(None if result.get('accepted') else '수신값 미채택: '+str(result.get('reason','리더 연결 대기')))
            except OSError as exc:
                # Replaceable telemetry: retry the newest sample, not the old one.
                # Follower/control state is maintained by the independent state poll.
                self.leader_upload_status(str(exc));self.stop.wait(.05)
            except Exception as exc:self.fail('PC 리더 → Pi 응답 오류: '+str(exc));return
            self.stop.wait(max(.001,.02-(time.monotonic()-began)))
    def relinquish(self):
        """The Pi acknowledged takeover; old PC cleanup must not send commands."""
        self.lease=None
        self.fail('Pi 안전 종료 작업으로 제어권 인계 완료')
    def close(self,disconnect=True):
        if self.leader_client:self.leader_client.close()
        if disconnect and self.lease and not self.error:
            try:self.rpc('disconnect')
            except (ValueError,OSError):pass
        self.stop.set()
        if self.lease and not self.error:
            try:self.http('/detach',{'lease':self.lease},timeout=1.)
            except (ValueError,OSError):pass
        if self.tunnel:
            self.tunnel.terminate()
            try:self.tunnel.wait(2)
            except subprocess.TimeoutExpired:self.tunnel.kill()

class RemoteMotionSession(MotionSession):
    def __init__(self,link,port,calibration,**kw):
        super().__init__(port,calibration,**kw);self.link=link;self.started_at=0.
    def start(self):
        self.link.rpc('connect',{'speed':self.rate_ticks_s});self.link.follower_requested=True;self.started_at=time.monotonic();self.running=True;self.link.listeners['follower']=self
    def consume(self,state,offset,received):
        if self.link.error or received<self.started_at or not state:return
        self.state=state['state'];self.error=state['error'];self.index=state['index'];self.active_request_id=state['request_id'];self.completed_request_id=state['completed_request_id'];self.grip_contact.hold_tick=state['gripper_hold_tick']
        for key in ('program_active','command_pending'):
            (getattr(self,key).set if state[key] else getattr(self,key).clear)()
        self.latest=snapshot(state['latest'],offset)
        if self.latest:self.publish('sample',self.latest)
        was_running=self.running;self.running=bool(state['running'])
        if was_running and not self.running:self.publish('closed',self.error)
    def event(self,kind,value):self.publish(kind,value)
    def failed(self,message):self.error=message;self.running=False;self.program_active.clear();self.command_pending.clear();self.publish('error',message);self.publish('closed',message)
    def request(self,action,targets=None):
        if not self.running:raise ValueError('Pi 팔로워를 먼저 연결하세요.')
        if targets:targets=[self.calibration.ticks(t) for t in targets]
        result=self.link.rpc('command',{'action':action,'targets':targets});self.started_at=time.monotonic()
        if action in ('move','play'):self.program_active.set()
        if action in ('arm','move','play','follow'):self.command_pending.set()
        return result.get('request_id')
    def set_speed(self,rate):
        super().set_speed(rate)
        if self.running:self.link.rpc('speed',{'rate':rate})
    def close(self):
        self.link.follower_requested=False
        if self.running:
            try:self.link.rpc('disconnect')
            except (ValueError,OSError):self.failed('Pi 연결 종료 응답 없음 · Pi에서 토크 상태를 확인하세요.')
    def join(self,timeout=2):return not self.running

from .leader_assist import LeaderAssistSession

class RemoteLeaderSession(LeaderAssistSession):
    """Leader USB stays on the PC; only calibrated samples cross the tunnel."""
    def __init__(self,link,port,calibration,**kw):super().__init__(port,calibration,**kw);self.link=link
    def start(self):
        self.link.rpc('leader_start');super().start();self.link.leader_client=self

class CameraTransport:
    """Independent SSH TCP stream: video retransmits cannot queue control bytes."""
    def __init__(self,link):self.link=link;self.tunnel=None;self.base=None;self.checked=False;self.after_at=None;self.after_preview_at=None
    def request(self):
        link=self.link
        # In-process HTTP fixtures have no SSH server port.
        if not hasattr(link,'server_port'):return link.http('/camera',{},timeout=1.2)
        if self.tunnel is None or self.tunnel.poll() is not None:
            self.close();c=link.config
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            self.base=f'http://127.0.0.1:{port}';self.checked=False
            args=ssh_args(c)+['-o','ControlMaster=no','-o','ControlPath=none','-o','ExitOnForwardFailure=yes','-N','-L',f'127.0.0.1:{port}:127.0.0.1:{link.server_port}','-l',c['user'],c['host']]
            self.tunnel=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if not self.checked:
            health=link.http('/health',{},timeout=1.2,base=self.base)
            if health['instance']!=link.instance:raise ValueError('Pi 카메라 실행부가 변경되었습니다.')
            self.checked=True
        value=link.http('/camera',{'after_at':self.after_at,'after_preview_at':self.after_preview_at},timeout=1.2,base=self.base)
        if value.get('image'):self.after_at=value.get('at')
        if value.get('preview_image') or value.get('image') and value.get('preview_at',value.get('at'))==value.get('at'):self.after_preview_at=value.get('preview_at',value.get('at'))
        return value
    def close(self):
        if self.tunnel:
            self.tunnel.terminate()
            try:self.tunnel.wait(1)
            except subprocess.TimeoutExpired:self.tunnel.kill();self.tunnel.wait(1)
            self.tunnel=None

class RemoteCameraSession(CameraSession):
    def __init__(self,link,*,observer=False):
        super().__init__('Pi');self.link=link;self.observer=observer;self.transport=CameraTransport(link);self.recovering=False;self.transport_delay=None
    def start(self):
        if self.running:return
        self.running=True;self.stop.clear();self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        import cv2,numpy as np
        rate=10.;failure_at=None;last_at=None;last_preview_at=None;sent_mode=None
        try:
            with self.frame_lock:sent_mode=(self.processing_enabled,self.preview_fps,self.processor_revision)
            mode_args={'processing_enabled':sent_mode[0],'preview_fps':sent_mode[1]}
            if not self.observer:
                if sent_mode[:2]==(True,10):self.link.camera_rpc('camera_start')
                else:self.link.camera_rpc('camera_start',mode_args)
            while not self.stop.is_set() and not self.link.error:
                start=time.monotonic()
                try:
                    with self.frame_lock:mode=(self.processing_enabled,self.preview_fps,self.processor_revision)
                    if mode!=sent_mode and not self.observer:
                        self.link.camera_rpc('camera_mode',{'processing_enabled':mode[0],'preview_fps':mode[1]});sent_mode=mode
                        last_at=None
                        self.transport.after_at=None
                    data=self.transport.request()
                except OSError as exc:
                    if isinstance(exc,HTTPError) and exc.code in (401,403):raise
                    # Camera retries never acquire a robot lease or fail control.
                    # Clear the observation so a delayed picture cannot start a move.
                    self.observation=None;self.frame=None;self.preview_frame=None;last_at=None;last_preview_at=None;self.recovering=True;self.transport_delay=str(exc)
                    if failure_at is None:failure_at=start
                    if time.monotonic()-failure_at>=5.:raise
                    if isinstance(self.transport,CameraTransport):self.transport.after_at=None;self.transport.after_preview_at=None
                    self.stop.wait(.2);continue
                failure_at=None;self.recovering=False;self.transport_delay=None
                elapsed=time.monotonic()-start
                rate=mode[1]
                with self.frame_lock:
                    if data['generation']>=self.link.generation and mode[2]==self.processor_revision:
                        offset=start-data['server_now']
                        def decode(encoded):
                            frame=cv2.imdecode(np.frombuffer(base64.b64decode(encoded),np.uint8),cv2.IMREAD_COLOR)
                            if frame is None:raise ValueError('Pi 영상 해독 실패')
                            return frame
                        if mode[0] and data.get('image') and data.get('at')!=last_at:
                            frame=decode(data['image']);at=data['at']+offset
                            self.observation=(frame,translate_detection(data['detection'],offset),at);last_at=data['at']
                        elif mode[0] and data.get('detection') and self.observation and data.get('at')==last_at:
                            # Timer finalization changes metadata, never capture age.
                            self.observation=(self.observation[0],translate_detection(data['detection'],offset),self.observation[2])
                        preview_at=data.get('preview_at',data.get('at'))
                        if preview_at is not None and preview_at!=last_preview_at:
                            frame=decode(data['preview_image']) if data.get('preview_image') else self.observation[0] if self.observation and preview_at==last_at else None
                            if frame is not None:
                                at=preview_at+offset;self.preview_frame=(frame,at);self.frame=frame;self.frame_at=at;last_preview_at=preview_at
                self.error=data['error']
                if not data['running']:
                    if not self.observer:break
                    self.observation=None;self.frame=None;self.preview_frame=None
                self.stop.wait(max(.01,1/rate-elapsed))
        except Exception as exc:
            self.error='Pi 카메라 통신 오류: '+str(exc)
        finally:
            self.recovering=False
            if not self.link.error and not self.observer:
                try:self.link.camera_rpc('camera_stop')
                except (ValueError,OSError) as exc:
                    if not self.error:self.error='Pi 카메라 해제 확인 지연: '+str(exc)
            self.transport.close();self.running=False
    def close(self):
        # UI navigation must never block on a network round trip. The receiver
        # owns shutdown; running stays True until its stop request is finished.
        self.stop.set()

class RemoteDetector(MultiDetector):
    def __init__(self,app,link):
        super().__init__(app.catalog,app.profile,app.active_jig,app.pose_latch);self.app=app;self.link=link;self.seconds=app.pose_latch.seconds;self.refresh()
    def clear(self,key=None):
        if self.frozen:return False
        self.link.sync(self.app)
        result=self.link.rpc('detect_clear',{'jig':key});self.link.generation=result['generation']
        if self.app.camera:self.app.camera.observation=None
        return super().clear(key)
    def freeze(self,enabled):
        owner=getattr(self.link,'execution_owner',None)
        if owner and owner.busy:
            super().freeze(True);return
        if self.frozen!=bool(enabled) and not self.link.error:self.link.rpc('detect_freeze',{'enabled':bool(enabled)})
        super().freeze(enabled)

class RemoteCommands:
    def __init__(self,worker):self.worker=worker
    def put(self,value):self.worker.link.rpc('cal_command',{'command':value})

class RemoteCalibrationWorker:
    def __init__(self,link,role,port,target,preset=None):
        self.link=link;self.role=role;self.port=port;self.target=Path(target);self.destination=self.target;self.preset=preset;self.running=False;self.state='IDLE';self.events=queue.Queue();self.commands=RemoteCommands(self);self.started_at=0
    def start(self):
        preset=None
        if self.preset:
            p=Path(self.preset);side=p.with_suffix('.angles.json');preset={'json':base64.b64encode(p.read_bytes()).decode(),'angles':base64.b64encode(side.read_bytes()).decode() if side.exists() else None}
        self.link.listeners['calibration']=self;self.started_at=time.monotonic();self.running=True
        try:self.link.rpc('cal_start',{'role':self.role,'port':self.port,'preset':preset})
        except Exception:self.running=False;raise
    def consume(self,state,offset,received):
        if state and received>=self.started_at:self.running=state['running'];self.state=state['state']
    def event(self,kind,value):
        if kind=='saved':
            self.target.parent.mkdir(parents=True,exist_ok=True);self.target.write_bytes(base64.b64decode(value['json'],validate=True))
            if value['angles']:self.target.with_suffix('.angles.json').write_bytes(base64.b64decode(value['angles'],validate=True))
            value=str(self.target)
        self.events.put((kind,value))
    def failed(self,message):self.running=False;self.events.put(('error',message))
    def close(self):
        if self.running:
            try:self.link.rpc('cal_stop')
            except (OSError,ValueError):self.failed('Pi 보정 연결이 끊겼습니다. 재접속 후 복원 상태를 확인하세요.')
