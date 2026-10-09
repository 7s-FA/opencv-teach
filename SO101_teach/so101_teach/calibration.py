"""Explicit hand-guided calibration; this worker never writes a movement target."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import queue,threading,time,math,json,hashlib,statistics
from .domain import JOINTS,LABELS,Calibration,Motor,atomic_json,read_json
from .angle_mapping import AngleMapping,angle_path,points_ready

def validate_progress(document,port):
    from .motor_backend import motor_backend
    MotorCalibration=motor_backend()[2]
    if document.get('schema')!=1 or Path(document.get('port','')).resolve()!=Path(port).resolve():raise ValueError('진행 기록의 로봇 포트가 다릅니다.')
    json.dumps(document,allow_nan=False)
    rows=document.get('firmware_calibration',{})
    if set(rows)!=set(JOINTS):raise ValueError('진행 기록에 6개 모터 설정이 필요합니다.')
    for i,n in enumerate(JOINTS,1):
        r=rows[n]
        if any(type(r.get(k)) is not int for k in ('id','drive_mode','homing_offset','range_min','range_max')) or r['id']!=i or r['drive_mode']!=0 or not -2047<=r['homing_offset']<=2047 or not 0<=r['range_min']<r['range_max']<=4095:raise ValueError('진행 기록의 모터 설정 오류: '+n)
    points=document.get('points',{})
    if not isinstance(points,dict) or not set(points)<=set(JOINTS) or set(document.get('middle',{}))!=set(points) or set(document.get('offsets',{}))!=set(points):raise ValueError('진행 기록의 관절 기준이 일치하지 않습니다.')
    for n,p in points.items():
        z=p.get('zero',{});tick=z.get('ticks');grip=n=='gripper' and p.get('kind')=='gripper_range'
        if type(tick) is not int or abs(tick-2047)>5 or z.get('degrees')!=(-90 if grip else 0) or document['middle'][n]!=tick or document['offsets'][n]!=rows[n]['homing_offset']:raise ValueError('진행 기록의 영점 오류: '+n)
        for side in ('negative','positive','closed','open'):
            if side not in p:continue
            v=p[side].get('ticks')
            if type(v) is not int or not 0<=v<=4095 or not (v<=tick if side=='closed' else v<tick if side=='negative' else v>tick):raise ValueError('진행 기록의 틱 방향 오류: '+n)
            if side in ('negative','positive'):
                deg=p[side].get('degrees')
                if grip or type(deg) not in (int,float) or not (-180<=deg<0 if side=='negative' else 0<deg<=180):raise ValueError('진행 기록의 기준각 오류: '+n)
            elif not grip:raise ValueError('닫힘·열림 기록은 집게에만 사용할 수 있습니다.')
    for key in ('mins','maxs'):
        values=document.get(key,{})
        if not isinstance(values,dict) or not set(values)<=set(JOINTS) or any(type(v) is not int or not 0<=v<=4095 for v in values.values()):raise ValueError('진행 기록의 범위 오류')
    return {n:MotorCalibration(**row) for n,row in rows.items()}
from .devices import new_bus,ensure_available,ReadOnlyViolation
from .communication import Communication,CommunicationCancelled,install_calibration_write_recovery

def install_calibration_gate(port,counts):
    original=port.writePort
    def write(packet):
        if len(packet)<6 or bytes(packet[:2])!=b'\xff\xff' or len(packet)!=packet[3]+4 or sum(packet[2:])%256!=255:raise ReadOnlyViolation('보정 패킷 오류')
        op=packet[4]
        if op not in (1,2,0x82):
            if op!=3 or packet[2] not in range(1,7) or len(packet)<8:raise ReadOnlyViolation('보정 외 쓰기 차단')
            addr=packet[5];data=bytes(packet[6:-1]);limits={9:2,11:2,31:2,33:1,40:1,55:1}
            if addr not in limits or len(data)!=limits[addr] or addr==40 and data!=b'\0' or addr==33 and data[0]>3 or addr==55 and data not in (b'\0',b'\1'):
                raise ReadOnlyViolation('보정 중 이동·토크 켜기는 허용하지 않습니다.')
        counts[op]+=1;return original(packet)
    port.writePort=write

class CalibrationWorker:
    def __init__(self,port,calibration,destination,preset=None,bus_factory=None,progress_path=None,resume=None):
        self.port=port;self.calibration=calibration;self.destination=Path(destination);self.preset=preset;self.bus_factory=bus_factory
        self.events=queue.Queue();self.commands=queue.Queue();self.stop=threading.Event();self.running=False;self.state='IDLE';self.saved=False;self.latest=None;self.cleanup_error=None;self.communication=Communication();self.communication.cancel=self.check_cancel
        self.points={};self.verification={};self.mins={};self.maxs={};self.middle={};self.offsets={};self.mapping=None;self.created_files=[]
        self.progress_path=Path(progress_path) if progress_path else None;self.resume=deepcopy(resume);self.last_checkpoint=0.
    def check_cancel(self):
        if self.stop.is_set():raise CommunicationCancelled('사용자 보정 종료 요청')
    def emit(self,kind,value):self.events.put((kind,value))
    def transition(self,state):self.state=state;self.emit('state',state)
    def start(self):self.running=True;self.thread=threading.Thread(target=self.run,daemon=False);self.thread.start()
    def close(self):self.stop.set()
    def run(self):
        bus=None;previous=None;modes={};changed=False;counts=Counter();recovery=self.destination.with_suffix('.recovery.json')
        try:
            from .motor_backend import motor_backend
            MotorCalibration=motor_backend()[2]
            resumed=validate_progress(self.resume,self.port) if self.resume else None
            if self.destination.exists() or angle_path(self.destination).exists():raise ValueError('기존 보정을 덮어쓰지 않습니다. 새 저장 경로를 사용하세요.')
            if self.bus_factory:bus=self.bus_factory()
            else:
                ensure_available(self.port);bus=new_bus(self.port,self.calibration,counts,gate_installer=install_calibration_gate)
            bus.communication=self.communication;install_calibration_write_recovery(bus,self.communication)
            bus.connect();previous=deepcopy(bus.read_calibration());modes={n:bus.read('Operating_Mode',n,normalize=False) for n in JOINTS}
            atomic_json(recovery,{'previous':{n:asdict(v) for n,v in previous.items()},'modes':modes,'port':self.port,'state':'started'})
            changed=True;bus.disable_torque()
            for n in JOINTS:
                if bus.read('Torque_Enable',n,normalize=False)!=0:raise RuntimeError(n+': 토크 OFF 확인 실패')
                bus.write('Operating_Mode',n,0,normalize=False)
            if self.preset:
                validated=Calibration(self.preset);draft={n:MotorCalibration(**row) for n,row in read_json(self.preset).items()};self.commit(bus,draft,recovery,validated.angle_mapping.document if validated.angle_mapping else None);return
            if resumed:
                if previous!=resumed:
                    bus.write_calibration(resumed)
                    if bus.read_calibration()!=resumed:raise RuntimeError('진행 기록의 모터 설정 적용 확인 실패')
                for key in ('points','middle','offsets','mins','maxs','verification'):setattr(self,key,deepcopy(self.resume.get(key,{})))
            self.transition('ANGLES' if self.points else 'MIDPOINT');last={}
            while not self.stop.is_set():
                positions=self.read_positions(bus)
                if self.state=='RANGE':
                    for n,v in positions.items():
                        if n=='gripper' and self.points.get(n,{}).get('kind')=='gripper_range':continue
                        if abs(v-last[n])>2048:raise ValueError(n+': 0/4095 경계를 넘었습니다. 중간 자세를 다시 맞추세요.')
                        self.mins[n]=min(self.mins[n],v);self.maxs[n]=max(self.maxs[n],v)
                    last=positions.copy()
                    if time.monotonic()-self.last_checkpoint>1:self.checkpoint(bus)
                self.latest={'current':positions,'mins':self.mins.copy(),'maxs':self.maxs.copy(),'points':deepcopy(self.points),'verification':deepcopy(self.verification)}
                self.emit('positions',self.latest)
                try:command=self.commands.get_nowait()
                except queue.Empty:command=None
                action=command[0] if isinstance(command,tuple) else command
                try:
                    if action=='zero' and self.state in ('MIDPOINT','ANGLES'):
                        if not isinstance(command,tuple) or len(command)!=2 or command[1] not in JOINTS:raise ValueError('영점을 기록할 관절을 선택하세요.')
                        name=command[1];self.sample_tick(bus,name)
                        try:
                            offsets=bus.set_half_turn_homings([name]);value=self.sample_tick(bus,name)
                            if set(offsets)!={name} or abs(value-2047)>5:raise RuntimeError('선택 관절의 2047틱 적용 확인 실패')
                        except CommunicationCancelled:raise
                        except Exception as exc:raise RuntimeError('관절 영점 기록 실패 · 이전 설정을 복원합니다: '+str(exc)) from exc
                        self.offsets[name]=offsets[name];self.middle[name]=value
                        self.points[name]={'zero':{'degrees':-90. if name=='gripper' else 0.,'ticks':value}}
                        if name=='gripper':self.points[name]['kind']='gripper_range'
                        self.verification.pop(name,None);self.mins.pop(name,None);self.maxs.pop(name,None);self.mapping=None
                        self.transition('ANGLES')
                        self.emit('notice',LABELS[JOINTS.index(name)]+f' 기준 기록 완료 · {value}틱 · '+('닫힘·열림을 기록하세요.' if name=='gripper' else '양쪽 각도를 다시 기록하세요.'))
                    elif action=='grip_endpoint' and self.state=='ANGLES':
                        _,side=command;p=deepcopy(self.points.get('gripper',{}))
                        if p.get('kind')!='gripper_range' or side not in ('closed','open'):raise ValueError('집게의 −90° 기준 자세를 먼저 기록하세요.')
                        value=self.sample_tick(bus,'gripper');anchor=p['zero']['ticks']
                        if not (value<=anchor if side=='closed' else value>anchor):raise ValueError('닫힘 틱은 기준 이하, 열림 틱은 기준보다 커야 합니다. 기록하지 않았습니다.')
                        p[side]={'ticks':value};self.points['gripper']=p;self.verification.pop('gripper',None);self.emit('notice',f"집게 {'닫힘(최소)' if side=='closed' else '열림(최대)'} {value}틱 기록 완료")
                    elif action=='record' and self.state in ('MIDPOINT','ANGLES'):
                        _,name,side,deg=command
                        if name not in JOINTS or side not in ('negative','positive') or type(deg) not in (int,float) or not math.isfinite(deg) or not (0<deg<=180 if side=='positive' else -180<=deg<0):raise ValueError('기준각의 부호와 범위(±180° 이내)를 확인하세요.')
                        if name=='gripper':raise ValueError('집게는 각도 대신 닫힘·열림 틱을 기록하세요.')
                        if 'zero' not in self.points.get(name,{}):raise ValueError('선택한 관절의 0°를 먼저 기록하세요.')
                        value=self.sample_tick(bus,name);candidate=deepcopy(self.points[name]);candidate[side]={'degrees':float(deg),'ticks':value}
                        tz=candidate['zero']['ticks']
                        if not (value<tz if side=='negative' else value>tz):
                            direction='작아야' if side=='negative' else '커야'
                            raise ValueError(f'기록하지 않았습니다. 현재 {value}틱은 영점 {tz}틱보다 {direction} 합니다.')
                        self.points[name]=candidate;self.verification.pop(name,None);self.emit('notice',LABELS[JOINTS.index(name)]+' 기준각 기록 완료')
                    elif action=='range' and self.state=='ANGLES':
                        if any(not points_ready(n,self.points.get(n,{})) for n in JOINTS):raise ValueError('몸체 5개 관절의 3점과 집게의 기준·닫힘·열림을 모두 기록하세요.')
                        self.mins={n:min(p['ticks'] for p in self.points[n].values() if isinstance(p,dict)) for n in JOINTS};self.maxs={n:max(p['ticks'] for p in self.points[n].values() if isinstance(p,dict)) for n in JOINTS}
                        last=positions.copy();self.transition('RANGE')
                    elif action=='angles' and self.state in ('RANGE','VERIFY'):
                        self.mapping=None;self.verification={};self.transition('ANGLES')
                    elif action=='review' and self.state=='RANGE':
                        _,document=self.draft();self.mapping=AngleMapping(document,document['calibration_sha256'],self.measured_motors());self.transition('VERIFY')
                    elif action=='verify' and self.state=='VERIFY':
                        _,name,deg=command
                        if name=='gripper':raise ValueError('집게는 각도 비교 대신 닫힘·열림 틱을 확인하세요.')
                        if name not in JOINTS or type(deg) not in (int,float) or not math.isfinite(deg) or not -180<=deg<=180:raise ValueError('검증할 관절·실제 각도를 확인하세요.')
                        if any(abs(deg-p['degrees'])<1e-6 for p in self.points[name].values()):raise ValueError('보정에 사용하지 않은 다른 각도를 입력하세요.')
                        tick=self.sample_tick(bus,name);m=self.measured_motors()[name]
                        if not m.low<=tick<=m.high:raise ValueError('검증 자세가 측정 범위 밖입니다. 움직임 범위를 다시 측정하세요.')
                        calculated=self.mapping.degrees(name,tick)
                        self.verification.setdefault(name,[]).append({'degrees':float(deg),'ticks':tick,'calculated_degrees':calculated,'error_degrees':calculated-deg})
                        self.verification[name]=self.verification[name][-20:];self.emit('notice',f'{LABELS[JOINTS.index(name)]}: 각도 차이 {calculated-deg:+.2f}°')
                    elif action=='save' and self.state=='VERIFY':
                        draft,document=self.draft();self.commit(bus,draft,recovery,document);break
                    elif action is not None:raise ValueError('현재 단계에서 사용할 수 없는 명령입니다.')
                    if action is not None and action!='save':self.checkpoint(bus)
                except ValueError as exc:self.emit('notice',str(exc))
                self.stop.wait(.05)
        except CommunicationCancelled:pass
        except Exception as exc:self.emit('error',str(exc))
        finally:
            if bus is not None:
                self.communication.cleaning=True
                try:
                    bus.disable_torque()
                    if changed and not self.saved and previous is not None:
                        bus.write_calibration(previous)
                        for n,v in modes.items():bus.write('Operating_Mode',n,v,normalize=False)
                        if bus.read_calibration()!=previous:raise RuntimeError('이전 보정 복원 실패')
                    # Lock EEPROM after the transaction.
                    for n in JOINTS:bus.write('Lock',n,1,normalize=False)
                except Exception as exc:self.cleanup_error=str(exc);self.emit('error','보정 정리 실패: '+str(exc)+' · 복원 기록: '+str(recovery))
                finally:bus.port_handler.closePort()
            if recovery.exists():
                try:
                    evidence=read_json(recovery);evidence['communication_events']=list(self.communication.events);evidence['communication_recoveries']=self.communication.recoveries;atomic_json(recovery,evidence)
                except OSError as exc:self.emit('error','보정 진단 기록 저장 실패: '+str(exc))
            if not self.saved:
                for path in self.created_files:
                    try:path.unlink(missing_ok=True)
                    except OSError as exc:self.emit('error','미완료 파일 정리 실패: '+str(exc))
            self.running=False;self.transition('FAULT' if self.cleanup_error else 'DONE' if self.saved else 'CLOSED')
    def read_positions(self,bus):
        return self.calibration.ticks({n:int(v) for n,v in bus.sync_read('Present_Position',normalize=False).items()},within_limits=False)
    def sample_tick(self,bus,name):
        values=[]
        for i in range(3):
            self.check_cancel();values.append(self.read_positions(bus)[name])
            if i<2:self.stop.wait(.015)
        if max(values)-min(values)>8:raise ValueError('관절이 움직이고 있습니다. 잠깐 멈춘 뒤 기록하세요.')
        return int(statistics.median(values))
    def measured_motors(self):
        return {n:Motor(self.calibration.motors[n].id,self.offsets[n],self.mins[n],self.maxs[n]) for n in JOINTS}
    def checkpoint(self,bus):
        if not self.progress_path:return
        try:
            doc={'schema':1,'port':self.port,'saved_at':time.time(),'firmware_calibration':{n:asdict(v) for n,v in bus.read_calibration().items()}}
            doc.update({k:deepcopy(getattr(self,k)) for k in ('points','middle','offsets','mins','maxs','verification')})
            validate_progress(doc,self.port);atomic_json(self.progress_path,doc);self.last_checkpoint=time.monotonic()
        except CommunicationCancelled:raise
        except Exception as exc:raise RuntimeError('보정 진행 기록 저장 실패: '+str(exc)) from exc
    def draft(self):
        from .motor_backend import motor_backend
        MotorCalibration=motor_backend()[2]
        if any(n not in self.middle or n not in self.offsets or n not in self.mins or n not in self.maxs for n in JOINTS):raise ValueError('6개 관절의 영점·각도·범위를 모두 기록하세요.')
        if not all(0<=self.mins[n]<=self.middle[n]<self.maxs[n]<=4095 and (n=='gripper' or self.mins[n]<self.middle[n]) for n in JOINTS):raise ValueError('몸체 영점 양쪽 범위와 집게 닫힘·열림 범위를 확인하세요.')
        draft={n:MotorCalibration(id=self.calibration.motors[n].id,drive_mode=0,homing_offset=self.offsets[n],range_min=self.mins[n],range_max=self.maxs[n]) for n in JOINTS}
        payload=json.dumps({n:asdict(v) for n,v in draft.items()},ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        document={'schema':1,'calibration_sha256':hashlib.sha256(payload.encode()).hexdigest(),'joints':deepcopy(self.points),'verification':deepcopy(self.verification)}
        AngleMapping(document,document['calibration_sha256'],self.measured_motors())
        return draft,document
    def commit(self,bus,draft,recovery,angles=None):
        if self.stop.is_set():return
        document={n:asdict(v) for n,v in draft.items()}
        payload=json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False)+'\n';sha=hashlib.sha256(payload.encode()).hexdigest()
        if angles:
            angles=deepcopy(angles);angles['calibration_sha256']=sha
            motors={n:Motor(v.id,v.homing_offset,v.range_min,v.range_max) for n,v in draft.items()};AngleMapping(angles,sha,motors)
        bus.write_calibration(draft)
        if bus.read_calibration()!=draft:raise RuntimeError('모터에 쓴 영점과 읽은 영점이 다릅니다.')
        # The calibration JSON is the package commit marker; sidecar is staged first.
        if angles:
            path=angle_path(self.destination);self.created_files.append(path);atomic_json(path,angles)
        self.created_files.append(self.destination);atomic_json(self.destination,document);self.saved=True
        evidence=read_json(recovery);evidence.update(state='saved',calibration=document,angle_file=str(angle_path(self.destination)) if angles else None);atomic_json(recovery,evidence)
        self.emit('saved',str(self.destination))
        if self.progress_path:
            try:self.progress_path.unlink(missing_ok=True)
            except OSError as exc:self.emit('notice','최종 저장 완료 · 진행 기록 정리 실패: '+str(exc))
