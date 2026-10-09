"""Single-owner read-only serial service. Wire-level WRITE instructions are blocked."""
from collections import Counter,deque
from copy import deepcopy
from pathlib import Path
import os
import queue
import subprocess
import threading
import time
from .domain import JOINTS, LABELS, Snapshot, atomic_json
from .communication import Communication,CommunicationCancelled,install_packet_recovery

class ReadOnlyViolation(RuntimeError):pass

def decode_telemetry(data):
    """STS feedback block 40..70; internal trajectory target is at 67, not 71."""
    if len(data)!=31:raise ValueError('서보 피드백은 31바이트가 필요합니다.')
    def word(i):return int.from_bytes(bytes(data[i:i+2]),'little')
    speed=word(18)
    return word(16),{'torque':data[0],'goal_ticks':word(2),'voltage_v':data[22]/10,'temperature_c':data[23],
        'status':data[25],'load_raw':word(20),'current_raw':word(29),'internal_goal_ticks':word(27),
        'velocity_signed_raw':-(speed&0x7fff) if speed&0x8000 else speed,'moving':data[26]}

def install_read_gate(port_handler,counts):
    original=port_handler.writePort
    def read_requests_only(packet):
        # PING, READ, SYNC_READ. All writes, reset and ACTION are rejected here.
        instruction=int(packet[4]) if len(packet)>=6 else -1
        if (len(packet)<6 or bytes(packet[:2])!=b'\xff\xff' or len(packet)!=int(packet[3])+4
                or sum(packet[2:])%256!=255 or instruction not in (1,2,0x82)):
            raise ReadOnlyViolation(f'읽기 전용 연결에서 명령 0x{instruction:02x}을 차단했습니다.')
        counts[instruction]+=1
        return original(packet)
    port_handler.writePort=read_requests_only

def ensure_available(port):
    try:p=Path(port).resolve(strict=True)
    except FileNotFoundError as exc:raise ValueError('로봇 USB 포트를 찾지 못했습니다. USB 연결을 확인하고 설정·안내 → 로봇에서 포트를 선택하세요. '+str(port)) from exc
    if not p.is_char_device() or not os.access(p,os.R_OK|os.W_OK):raise ValueError('사용 가능한 시리얼 장치가 아닙니다.')
    result=subprocess.run(['fuser',str(p)],capture_output=True,timeout=4)
    if result.returncode==0:raise RuntimeError('다른 프로그램이 모터 포트를 사용 중입니다. 해당 연결을 먼저 해제하세요.')
    if result.returncode!=1:raise RuntimeError('시리얼 포트 사용 상태를 확인할 수 없습니다.')

def new_bus(port,calibration,counts,*,gate_installer=install_read_gate):
    # Hardware serial ownership is Linux-only; the offline GUI also runs on Windows.
    import fcntl
    import termios
    from .motor_backend import motor_backend
    FeetechMotorsBus,Motor,MotorCalibration,MotorNormMode=motor_backend()
    bus=FeetechMotorsBus(port,{n:Motor(m.id,'sts3215',MotorNormMode.DEGREES) for n,m in calibration.motors.items()},
        {n:MotorCalibration(id=m.id,drive_mode=0,homing_offset=m.homing,range_min=m.low,range_max=m.high) for n,m in calibration.motors.items()})
    # Firmware 3.10 feedback at 67..68 is the current trajectory target.
    # Keep the SDK's unrelated address-71 alias untouched; use our own verified name.
    bus.model_ctrl_table=deepcopy(bus.model_ctrl_table)
    bus.model_ctrl_table['sts3215']['Internal_Goal_Position']=(67,2)
    install_packet_recovery(bus,Communication())
    # Do not instantiate a robot's configure()/calibrate() lifecycle.
    gate_installer(bus.port_handler,counts)
    original=bus.port_handler.openPort
    def exclusive_open():
        result=original()
        if result:
            try:
                fd=bus.port_handler.ser.fileno()
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.ioctl(fd,termios.TIOCEXCL)
            except BaseException:bus.port_handler.closePort();raise
        return result
    bus.port_handler.openPort=exclusive_open
    return bus

class ReadOnlySession:
    def __init__(self,port,calibration,role='follower',audit_path=None,bus_factory=new_bus):
        self.port=port;self.calibration=calibration;self.role=role;self.audit_path=audit_path;self.bus_factory=bus_factory
        self.stop=threading.Event();self.events=queue.Queue(4);self.counts=Counter();self.thread=None
        self.latest=None;self.error=None;self.report={};self.running=False
        self.communication=Communication();self.communication.cancel=self.check_communication_cancel
        from .telemetry import VoltageTracker
        self.voltage=VoltageTracker(duration=None if role=='leader' else 5.)
        self.voltage_notice_seconds=.1;self.voltage_notified=set();self.communication.voltage_handler=self.packet_voltage
    def check_communication_cancel(self):
        if self.stop.is_set():raise CommunicationCancelled('사용자 연결 종료 요청')
    def publish(self,kind,value):
        try:self.events.put_nowait((kind,value))
        except queue.Full:
            try:self.events.get_nowait()
            except queue.Empty:pass
            self.events.put_nowait((kind,value))
    def start(self):
        if self.running:raise RuntimeError('이미 연결 중입니다.')
        self.running=True;self.thread=threading.Thread(target=self.run,name='SO101-read-only',daemon=True);self.thread.start()
    def on_snapshot(self,bus,snapshot):pass
    def before_close(self,bus):pass
    def observe_voltage(self,name,active,*,complete=False,value=None):
        if self.communication.cleaning:return
        now=time.monotonic();state=self.voltage.observe(name,active,now,complete=complete)
        if state=='warning':self.voltage_notified.discard(name)
        if active:
            counts=self.report.setdefault('voltage_warnings',{});counts[name]=counts.get(name,0)+1
        if state:
            self.report.setdefault('voltage_events',[]).append({'at':time.time(),'joint':name,'state':state,'voltage_v':value})
            self.report['voltage_events']=self.report['voltage_events'][-100:]
        role='리더' if self.role=='leader' else '팔로워'
        label=dict(zip(JOINTS,LABELS)).get(name,name)
        if active and name not in self.voltage_notified and now-self.voltage.pending.get(name,now)>=self.voltage_notice_seconds:
            self.voltage_notified.add(name)
            detail='위치 읽기는 계속합니다.' if self.role=='leader' else f'{self.voltage.duration:g}초 지속 시 중단합니다.'
            measured='' if value is None else f' · {value:.1f}V'
            self.publish('device_notice',f'{role} {label}: 전압 변동이 {self.voltage_notice_seconds:g}초 이상 지속됩니다{measured} · '+detail)
        elif state=='recovered' and name in self.voltage_notified:
            self.voltage_notified.discard(name)
            self.publish('device_notice',f'{role} {label}: 전압 변동 해제')
    def packet_voltage(self,motor):
        name=next((n for n,m in self.calibration.motors.items() if m.id==motor),None)
        if name is None:raise RuntimeError('알 수 없는 모터의 전압 경고')
        self.observe_voltage(name,True)
    def feedback(self,name,data,comm,error):
        if comm!=0 or len(data)!=31:raise RuntimeError(f'{name}: 통신 오류 (comm={comm}, alarm={error}, bytes={len(data)})')
        tick,health=decode_telemetry(data);health['packet_alarm']=error
        if error not in (0,1) or health['status'] not in (0,1):raise RuntimeError(f'{name}: 서보 경고 (alarm={error}, status={health["status"]})')
        self.observe_voltage(name,error==1 or health['status']==1,complete=True,value=health['voltage_v'])
        return tick,health
    def collect_snapshot(self,bus,matches):
        """Discard an over-age scan and collect anew; never relabel stale data."""
        began=time.monotonic();discarded=0
        while not self.stop.is_set():
            ticks={};health={};scan_started=time.monotonic()
            for name in JOINTS:
                if self.stop.is_set():return None
                motor=self.calibration.motors[name]
                data,comm,error=bus.packet_handler.readTxRx(bus.port_handler,motor.id,40,31)
                ticks[name],health[name]=self.feedback(name,data,comm,error)
            if self.stop.is_set():return None
            self.calibration.ticks(ticks,within_limits=False)
            snapshot=Snapshot(self.role,ticks,health,scan_started,time.time(),self.calibration.sha256,matches,self.port)
            now=time.monotonic();duration=now-scan_started
            self.report['max_scan_seconds']=max(self.report.get('max_scan_seconds',0.),duration)
            if snapshot.fresh(now) and (not discarded or now-began<=1.5):
                if discarded:
                    self.report['scan_recoveries']=self.report.get('scan_recoveries',0)+1
                    self.communication.record('전체 위치 재수집',discarded+1,'recovered')
                    self.publish('device_notice','모터 위치 수집 복구 · 최신 위치 확인')
                return snapshot
            discarded+=1
            events=self.report.setdefault('slow_scans',[])
            events.append({'at':time.time(),'duration_seconds':duration,'discarded':discarded})
            self.report['slow_scans']=events[-30:]
            if now-began>=1.5:
                raise ConnectionError('전체 모터의 최신 위치를 1.5초 안에 수집하지 못했습니다. 제어를 중단합니다.')
            if discarded==1:self.publish('device_notice','모터 위치 수집 지연 · 늦은 값은 버리고 다시 읽습니다.')
            # Existing servo goals remain untouched while fresh feedback is absent.
            self.stop.wait(.01)
        return None

    def run(self):
        bus=None;last_cal=0;last_history=0;matches=False;recent=deque(maxlen=300)
        self.report={'started_at':time.time(),'port':self.port,'role':self.role,'calibration_sha256':self.calibration.sha256,'samples':0,'writes':0}
        try:
            if self.bus_factory is new_bus or getattr(self,'requires_port_check',False):ensure_available(self.port)
            bus=self.bus_factory(self.port,self.calibration,self.counts);bus.communication=self.communication;bus.connect()
            while not self.stop.is_set():
                if time.monotonic()-last_cal>2:
                    actual=bus.read_calibration();matches=all(n in actual and actual[n].homing_offset==m.homing and actual[n].range_min==m.low and actual[n].range_max==m.high for n,m in self.calibration.motors.items())
                    self.report['calibration_matches']=matches;last_cal=time.monotonic()
                snapshot=self.collect_snapshot(bus,matches)
                if snapshot is None:break
                ticks=snapshot.ticks;health=snapshot.telemetry
                self.latest=snapshot;self.report['samples']+=1
                self.report['last']={'ticks':ticks,'telemetry':health,'time':snapshot.wall_time}
                recent.append(self.report['last'])
                if snapshot.monotonic-last_history>=1:
                    self.report.setdefault('telemetry_history',[]).append({'time':snapshot.wall_time,'ticks':ticks,'telemetry':health})
                    self.report['telemetry_history']=self.report['telemetry_history'][-600:];last_history=snapshot.monotonic
                self.on_snapshot(bus,snapshot)
                self.publish('sample',snapshot);self.stop.wait(getattr(self,'poll_seconds',.08))
        except CommunicationCancelled:
            pass
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}';self.report['error']=self.error;self.publish('error',self.error)
        finally:
            self.report['recent_samples']=list(recent)
            # SDK disconnect() may disable torque; close the descriptor only.
            if bus is not None:
                self.communication.cleaning=True
                try:self.before_close(bus)
                except Exception as exc:
                    self.report['cleanup_error']=str(exc);self.error='토크 해제 확인 실패: '+str(exc);self.publish('error',self.error)
                try:bus.port_handler.closePort()
                except Exception as exc:self.report['close_error']=str(exc)
            self.report['communication_recoveries']=self.communication.recoveries;self.report['communication_events']=list(self.communication.events)
            self.report['writes']=self.counts.get(3,0)+self.counts.get(0x83,0)
            if hasattr(self,'command_log'):self.report['command_trace']=list(self.command_log)
            self.report.update(finished_at=time.time(),request_instructions={hex(k):v for k,v in self.counts.items()})
            if self.audit_path:
                try:atomic_json(self.audit_path,self.report)
                except Exception as exc:self.publish('error','진단 기록 저장 실패: '+str(exc))
            self.running=False
            self.publish('closed',self.error)
    def close(self):self.stop.set()
    def join(self,timeout=2):
        if self.thread:self.thread.join(timeout)
        return not self.running

class CameraSession:
    def __init__(self,source,width=1280,height=720,processor=None):
        self.source=source;self.width=width;self.height=height;self.stop=threading.Event();self.processor=processor;self.observation=None
        self.frame=None;self.frame_at=0.;self.error=None;self.running=False;self.thread=None;self.max_fps=10
        self.preview_frame=None;self.processing_thread=None;self.frame_ready=threading.Event();self.frame_lock=threading.Lock();self.pending_frame=None;self.processor_revision=0
        self.processing_enabled=True;self.preview_fps=10
    @property
    def observation(self):
        obs=getattr(self,'_observation',None)
        owner=getattr(self.processor,'__self__',None)
        if obs and self.running and self.processing_enabled and hasattr(owner,'finish_observation'):
            return obs[0],owner.finish_observation(obs[1],time.monotonic()),obs[2]
        return obs
    @observation.setter
    def observation(self,value):self._observation=value
    @property
    def preview_observation(self):
        preview=self.preview_frame
        if preview is None:return self.observation
        frame,at=preview;obs=self.observation
        if not self.processing_enabled:
            return frame,{'selected':None,'candidates':[],'status':'paused','live_by_jig':{},'_preview':True,'detection_frame_at':None},at
        # Model detection on the Pi takes longer than one preview frame. Keep
        # its capture timestamp and expose its age; never refresh that timestamp
        # or feed these display-only results back into position acceptance.
        from .camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
        age=at-obs[2] if obs else float('inf')
        result=obs[1] if obs and 0<=age<=MEASUREMENT_MAX_AGE_SECONDS else {'selected':None,'candidates':[],'status':'processing','live_by_jig':{}}
        if result and obs and 0<=age<=MEASUREMENT_MAX_AGE_SECONDS:
            result={**result,'preview_detection_age_s':age}
            if 'live_by_jig' in result:
                result['live_by_jig']={key:{**value,'preview_detection_age_s':age} for key,value in result['live_by_jig'].items()}
        return frame,{**result,'_preview':True,'detection_frame_at':obs[2] if obs else None} if result else result,at
    def start(self):
        if self.running:return
        self.stop.clear();self.running=True;self.thread=threading.Thread(target=self.run,name='SO101-camera',daemon=True);self.thread.start()
    def set_processor(self,processor):
        with self.frame_lock:
            self.processor=processor;self.processor_revision+=1;self.observation=None;self.pending_frame=None
    def set_mode(self,*,processing_enabled,preview_fps):
        if type(processing_enabled) is not bool or preview_fps not in (5,10):raise ValueError('잘못된 카메라 갱신 설정')
        with self.frame_lock:
            if self.processing_enabled!=processing_enabled:
                self.processor_revision+=1;self.pending_frame=None
                if processing_enabled:self.observation=None
            self.processing_enabled=processing_enabled;self.preview_fps=preview_fps
    def process_frames(self):
        while not self.stop.is_set():
            self.frame_ready.wait(.1)
            with self.frame_lock:
                pending=self.pending_frame if self.processing_enabled else None;self.pending_frame=None;self.frame_ready.clear();processor=self.processor;revision=self.processor_revision
            if pending is None:continue
            frame,at=pending
            try:result=processor(frame) if processor else None
            except Exception as exc:result={'error':str(exc),'selected':None,'candidates':[],'status':'error'}
            with self.frame_lock:
                if not self.stop.is_set() and revision==self.processor_revision:self.observation=(frame,result,at)
    def run(self):
        import cv2
        cap=None
        try:
            source=int(self.source) if str(self.source).isdecimal() else self.source
            cap=cv2.VideoCapture(source,cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,self.width);cap.set(cv2.CAP_PROP_FRAME_HEIGHT,self.height);cap.set(cv2.CAP_PROP_BUFFERSIZE,1);cap.set(cv2.CAP_PROP_FPS,10)
            if not cap.isOpened():raise RuntimeError('카메라를 열 수 없습니다.')
            if self.processor:
                self.processing_thread=threading.Thread(target=self.process_frames,name='SO101-vision',daemon=True);self.processing_thread.start()
            failure_started=None
            while not self.stop.is_set():
                began=time.monotonic();ok,frame=cap.read()
                if not ok:
                    now=time.monotonic()
                    if failure_started is None:failure_started=now
                    if now-failure_started>=1.:raise RuntimeError('카메라 영상 수신이 1초 이상 중단되었습니다.')
                    self.stop.wait(.05);continue
                failure_started=None;at=time.monotonic()
                # Drain the device at its capture rate to avoid stale buffered
                # frames on resume; publish and transmit only at the view rate.
                if self.preview_frame is None or (self.max_fps and self.preview_fps>=self.max_fps) or at-self.preview_frame[1]>=1/self.preview_fps:
                    self.preview_frame=(frame,at);self.frame=frame;self.frame_at=at
                with self.frame_lock:
                    if self.processor and self.processing_enabled:self.pending_frame=(frame,at);self.frame_ready.set()
                    elif not self.processor:self.observation=(frame,None,at)
                self.stop.wait(max(0.,1/self.max_fps-(at-began)) if getattr(self,'max_fps',None) else .02)
        except Exception as exc:self.error=str(exc)
        finally:
            if cap is not None:cap.release()
            self.stop.set();self.frame_ready.set()
            if self.processing_thread:self.processing_thread.join(3)
            self.running=False
    def close(self):self.stop.set();self.frame_ready.set()
    def join(self,timeout=3):
        if self.thread:self.thread.join(timeout)
        return not self.running
