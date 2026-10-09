"""Explicit raw-tick commands in the serial owner thread. No IK/vision motion.

Connect is read-only. Only an explicit hold request can begin activation. Each
RAM write needs a one-shot wire permit; calibration/EEPROM/reset remain blocked.
"""
import math
import queue
import threading
import time
from collections import deque
from .domain import JOINTS,LABELS,atomic_json
from .devices import ReadOnlySession,new_bus,ReadOnlyViolation
from .gripper_contact import GripperContact,DEFAULT_CONTACT_LOAD
from .load_compensation import LoadCompensation,CorrectionStalled,MAX_LOAD
from .joint_path import JointPath

RAM={'Torque_Enable':(40,1),'Acceleration':(41,1),'Goal_Position':(42,2),
     'Goal_Time':(44,2),'Goal_Velocity':(46,2)}
RATE_TICKS_S=350.
SPEED_PRESETS={'느리게':300.,'보통':RATE_TICKS_S,'빠르게':400.}
FOLLOW_BODY_MARGIN_TICKS=8  # Keep body targets inside the recorded contact stops.
FOLLOW_RECEIVE_TIMEOUT=2.  # Brief gaps hold position inside FOLLOW, then require explicit restart.
FOLLOW_RATE_TICKS_S=750.  # Firmware velocity ceiling, about 66 deg/s.
FOLLOW_START_SPEEDS={'아주 느리게':100.,'느리게':200.,'보통':300.,'빠르게':SPEED_PRESETS['빠르게']}
DEFAULT_FOLLOW_START_RATE=SPEED_PRESETS['빠르게']
ARRIVAL_TOLERANCE_TICKS=30
BODY_ARRIVAL_TOLERANCE_TICKS=20
PASS_THROUGH_TOLERANCE_TICKS=32
EPISODE_RESIDUAL_LIMIT_TICKS=32

def leader_target(leader_ticks,leader_calibration,follower_calibration,*,with_limits=False):
    """Map body angles and gripper travel without changing either measured limit."""
    lc=leader_calibration;fc=follower_calibration;desired={};limited={}
    lc.ticks(leader_ticks,within_limits=False)
    for n,label in zip(JOINTS,LABELS):
        fm=fc.motors[n];lm=lc.motors[n]
        if n=='gripper':
            desired[n]=round(fm.low+(leader_ticks[n]-lm.low)/(lm.high-lm.low)*(fm.high-fm.low))
        else:
            degrees=lc.angle_mapping.degrees(n,leader_ticks[n]) if lc.angle_mapping else (leader_ticks[n]-2047)*360/4096
            desired[n]=round(fc.angle_mapping.ticks(n,degrees) if fc.angle_mapping else 2047+degrees*4096/360)
        margin=min(FOLLOW_BODY_MARGIN_TICKS,(fm.high-fm.low)//2) if n!='gripper' else 0
        low,high=fm.low+margin,fm.high-margin;requested=desired[n];desired[n]=max(low,min(high,requested))
        if requested!=desired[n]:limited[n]={'requested':requested,'applied':desired[n],'low':low,'high':high}
    desired=fc.ticks(desired)
    return (desired,limited) if with_limits else desired

def smooth_fraction(fraction):
    """Quintic S curve: zero endpoint velocity/acceleration, peak slope 1.875."""
    u=max(0.,min(1.,float(fraction)))
    return u*u*u*(10.+u*(-15.+6.*u))

def velocity_register(rate,phase):
    # Phase bit 2: ticks/s (50-tick quantization); otherwise units of 50 ticks/s.
    ceiling=math.ceil(rate*1.25/50)*50
    return ceiling if phase&4 else ceiling//50
RUNAWAY_TICKS=128  # 11.25 degrees; explicit large unexpected displacement guard.
INTERNAL_TARGET_MARGIN=8  # Firmware target must stay inside the commanded segment.
HEARTBEAT_TIMEOUT=2.
START_ORDER=('wrist_roll','gripper','shoulder_pan','shoulder_lift','elbow_flex','wrist_flex')

class MotionGate:
    def __init__(self,calibration):self.calibration=calibration;self.permit=None
    def allow(self,name,register,value):
        if register not in RAM or type(value) is not int:raise ReadOnlyViolation('허용되지 않은 모터 쓰기')
        motor=self.calibration.motors[name];address,length=RAM[register]
        legal={'Torque_Enable':value in (0,1),'Acceleration':value==0,'Goal_Time':value==0,'Goal_Velocity':value in (1,4,5,7,8,9,10,15,20,200,250,350,400,450,500,750,1000),
               'Goal_Position':motor.low<=value<=motor.high}
        if not legal[register]:raise ReadOnlyViolation('허용 범위 밖 모터 쓰기')
        self.permit=(motor.id,address,bytes(value.to_bytes(length,'little')))
    def allow_sync_goals(self,ticks):
        ticks=self.calibration.ticks(ticks)
        self.permit=('sync',bytes([42,2])+b''.join(bytes([self.calibration.motors[n].id])+ticks[n].to_bytes(2,'little') for n in ticks))
    def install(self,port,counts):
        original=port.writePort
        def guarded(packet):
            if (len(packet)<6 or bytes(packet[:2])!=b'\xff\xff' or len(packet)!=int(packet[3])+4 or sum(packet[2:])%256!=255):
                raise ReadOnlyViolation('잘못된 모터 패킷')
            op=int(packet[4])
            if op not in (1,2,0x82):
                permitted=self.permit;self.permit=None
                valid_sync=op==0x83 and packet[2]==254 and permitted==('sync',bytes(packet[5:-1]))
                if not valid_sync and (op!=3 or len(packet)<8 or permitted!=(int(packet[2]),int(packet[5]),bytes(packet[6:-1]))):
                    raise ReadOnlyViolation('승인되지 않은 모터 쓰기를 차단했습니다.')
            counts[op]+=1;return original(packet)
        port.writePort=guarded

def motion_bus(port,calibration,counts):
    gate=MotionGate(calibration);bus=new_bus(port,calibration,counts,gate_installer=gate.install);bus.motion_gate=gate;return bus

class MotionSession(ReadOnlySession):
    poll_seconds=.02
    def __init__(self,port,calibration,**kwargs):
        self.grip_contact=GripperContact(kwargs.pop('grip_contact_load',DEFAULT_CONTACT_LOAD))
        rate=kwargs.pop('rate_ticks_s',RATE_TICKS_S)
        if rate not in SPEED_PRESETS.values():raise ValueError('지원하지 않는 실물 속도')
        self.rate_ticks_s=float(rate)
        self.follow_start_rate=float(kwargs.pop('follow_start_rate_ticks_s',DEFAULT_FOLLOW_START_RATE))
        if self.follow_start_rate not in FOLLOW_START_SPEEDS.values():raise ValueError('지원하지 않는 최초 합류 속도')
        self.follow_initial=False;self.follow_initial_target={}
        factory=kwargs.pop('bus_factory',motion_bus)
        super().__init__(port,calibration,bus_factory=factory,**kwargs)
        self.requires_port_check=factory is motion_bus
        self.commands=queue.Queue(4);self.release_requested=threading.Event();self.hold_requested=threading.Event()
        self.state='READ_ONLY';self.heartbeat=time.monotonic();self.motion_attempted=False;self.last_goals={};self.enabled={}
        from .telemetry import TemperatureTracker
        self.temperature=TemperatureTracker();self.leader_provider=None;self.leader_calibration=None;self.follow_time=None;self.follow_last_valid_at=0.;self.follow_waiting=False;self.follow_recovering=False;self.follow_control_at=0.
        self.program_active=threading.Event();self.command_pending=threading.Event();self.request_serial=0;self.active_request_id=None;self.completed_request_id=None
        self.sequence=[];self.sequence_stops=[];self.episode_run=False;self.residual_reason=None;self.residual_since=None;self.index=0;self.segment=None;self.settled_at=None;self.motion_log=[]
        self.command_log=deque(maxlen=512);self.activation_log=deque(maxlen=512)
        self.joint_path=None;self.path_slot=0;self.path_segment=None;self.load_compensation=LoadCompensation();self.internal_paths={};self.last_snapshot=None;self.follow_progress={};self.sync_unconfirmed={}
    def set_speed(self,rate):
        if rate not in SPEED_PRESETS.values():raise ValueError('지원하지 않는 실물 속도')
        if self.state not in ('READ_ONLY','HOLD') or self.command_pending.is_set() or self.program_active.is_set():
            raise ValueError('실행이 끝난 뒤 속도를 바꾸세요.')
        self.rate_ticks_s=float(rate)
    @property
    def max_step_ticks(self):return max(6,math.ceil(self.rate_ticks_s*.1))
    def configure_motion_speed(self,bus,*,follow=False):
        # Executed only by the serial owner for an explicit move/follow request.
        for name in JOINTS:
            phase=self.read(bus,name,'Phase')
            value=(int(FOLLOW_RATE_TICKS_S) if phase&4 else int(FOLLOW_RATE_TICKS_S/50)) if follow else velocity_register(self.rate_ticks_s,phase)
            self.write(bus,name,'Goal_Velocity',value)
            if self.read(bus,name,'Goal_Velocity')!=value:raise RuntimeError(name+': 이동 속도 확인 실패')
        self.report['motion_rate_ticks_s']=self.rate_ticks_s
        self.active_rate=FOLLOW_RATE_TICKS_S if follow else self.rate_ticks_s
        if follow:self.report['follow_rate_ticks_s']=FOLLOW_RATE_TICKS_S
    def transition(self,state,detail='',*,paused=False):
        self.state=state;event={'at':time.time(),'state':state,'detail':detail}
        if paused:event['paused']=True
        self.motion_log.append(event);self.publish('motion',event)
    def request(self,action,targets=None):
        if not self.running:raise ValueError('팔로워를 먼저 연결하세요.')
        if action=='release':self.release_requested.set();return
        if action=='hold':self.hold_requested.set();return
        if action not in ('arm','move','play','follow'):raise ValueError('잘못된 실물 명령')
        if self.command_pending.is_set():raise ValueError('실물 명령 처리 중입니다.')
        if self.state!=('READ_ONLY' if action=='arm' else 'HOLD'):raise ValueError('현재 자세 유지 상태에서 이동을 실행하세요.')
        # Copy validated values; UI edits cannot change an in-flight program.
        values=[self.calibration.ticks(t) for t in (targets or [])]
        if action in ('move','play') and (not values or len(values)>500):raise ValueError('실행할 스텝을 선택하세요.')
        self.request_serial+=1;request_id=self.request_serial
        self.command_pending.set()
        if action in ('move','play'):self.program_active.set()
        try:self.commands.put_nowait((action,values,time.monotonic(),request_id))
        except queue.Full:
            self.command_pending.clear();self.program_active.clear()
            raise ValueError('실물 명령 처리 중입니다. 완료 후 다시 실행하세요.')
        return request_id
    def check_abort(self):
        if self.stop.is_set() or self.release_requested.is_set():raise RuntimeError('사용자 토크 해제/연결 종료 요청')
    def write(self,bus,name,register,value):
        off=register=='Torque_Enable' and value==0
        record={'at':time.time(),'joint':name,'register':register,'value':value,'acknowledged':False,'attempts':0};self.command_log.append(record)
        def send():
            if not off:self.check_abort()
            bus.motion_gate.allow(name,register,value);record['attempts']+=1
            try:bus.write(register,name,value,normalize=False,num_retry=0)
            finally:bus.motion_gate.permit=None
        def before_retry():
            if register=='Torque_Enable' and value==1:
                last=next((r for r in reversed(self.command_log) if r['joint']==name and r['register']=='Goal_Position' and (r['acknowledged'] or r.get('verified'))),None)
                if last is None or self.read(bus,name,'Goal_Position')!=last['value'] or abs(self.read(bus,name,'Present_Position')-last['value'])>RUNAWAY_TICKS:
                    raise RuntimeError(name+': 토크 재시도 전 현재 자세·목표 확인 실패')
        def execute():
            result=self.communication.verified_write(bus,name+' '+register,value,send,lambda:bus.read(register,name,normalize=False,num_retry=0),before_retry=before_retry,off=off)
            record['acknowledged']=result=='ack';record['verified']=result=='readback'
        if off:
            with self.communication.cleanup():execute()
        else:execute()
    def read(self,bus,name,register):
        self.check_abort();value=bus.read(register,name,normalize=False,num_retry=0)
        if register=='Status' and value==1:self.observe_voltage(name,True)
        return value
    def release(self,bus):
        with self.communication.cleanup():self._release(bus)
    def _release(self,bus):
        errors=[]
        for name in JOINTS:
            try:
                self.write(bus,name,'Torque_Enable',0)
                if self.communication.retry_read(bus,name+' 토크 OFF 확인',lambda:bus.read('Torque_Enable',name,normalize=False,num_retry=0))!=0:raise RuntimeError('OFF 미확인')
            except Exception as exc:errors.append(name+': '+str(exc))
        self.load_compensation.reset();self.enabled={};self.sequence=[];self.segment=None;self.last_goals={};self.grip_contact.reset();self.motion_attempted=bool(errors);self.program_active.clear();self.command_pending.clear()
        while not self.commands.empty():
            try:self.commands.get_nowait()
            except queue.Empty:break
        self.release_requested.clear();self.hold_requested.clear()
        if errors:self.transition('FAULT','; '.join(errors));raise RuntimeError('; '.join(errors))
        self.transition('READ_ONLY','토크 OFF 확인')
    def activation_sample(self,bus,name,goal,phase):
        began=time.monotonic()
        row={'at':time.time(),'joint':name,'phase':phase,'hold_goal_ticks':goal}
        self.activation_log.append(row)
        # Preserve even a partial read on a communication failure. These are
        # sequential register reads, not a fabricated simultaneous snapshot.
        self.report['activation_trace']=list(self.activation_log)
        for field,register in (('actual_ticks','Present_Position'),('command_goal_ticks','Goal_Position'),
                               ('internal_goal_ticks','Internal_Goal_Position'),('torque','Torque_Enable'),('status','Status')):
            row[field]=self.read(bus,name,register)
        row['scan_seconds']=time.monotonic()-began
        return row
    def check_enabled(self,bus):
        for name,goal in self.enabled.items():
            row=self.activation_sample(bus,name,goal,'activation_monitor')
            actual=row['actual_ticks']
            if abs(actual-goal)>RUNAWAY_TICKS:raise RuntimeError(f'{name}: 큰 위치 이탈 ({goal} → {actual}틱, 허용 {RUNAWAY_TICKS}틱)')
            if row['torque']!=1 or row['status'] not in (0,1):raise RuntimeError(name+': 시작 상태 이상')
            if row['command_goal_ticks']!=goal:raise RuntimeError(name+': 시작 목표 변경')
            self.check_internal_target(name,row['internal_goal_ticks'],goal,goal,
                                       actual=actual,command_goal=row['command_goal_ticks'])
    def check_internal_target(self,name,internal,start,goal,*,actual=None,command_goal=None):
        if not min(start,goal)-INTERNAL_TARGET_MARGIN<=internal<=max(start,goal)+INTERNAL_TARGET_MARGIN:
            self.report['internal_target_fault']={'at':time.time(),'joint':name,'phase':self.state,
                'actual_ticks':actual,'command_goal_ticks':command_goal,'internal_goal_ticks':internal,
                'segment_start_ticks':start,'segment_goal_ticks':goal,'allowed_margin_ticks':INTERNAL_TARGET_MARGIN}
            present='미확인' if actual is None else f'{actual}틱'
            written='미확인' if command_goal is None else f'{command_goal}틱'
            target=f'자동 유지 목표 {goal}틱' if self.state=='ACTIVATING' else f'명령 구간 {min(start,goal)}~{max(start,goal)}틱'
            raise RuntimeError(f'{name}: 서보 내부 목표 불일치 · 실물 현재 {present} · {target} · '
                               f'목표 레지스터 {written} · 내부 목표 {internal}틱 (허용 여유 ±{INTERNAL_TARGET_MARGIN}틱). '
                               '내부 목표값은 실물 위치가 아닙니다. 활성화·제어 진단 기록을 확인하세요.')
    def arm(self,bus,snapshot):
        if not snapshot.fresh() or not snapshot.calibration_matches:raise RuntimeError('최신 위치·영점 대조가 필요합니다.')
        initial=self.calibration.ticks(snapshot.ticks)
        if any(h['torque'] for h in snapshot.telemetry.values()):raise RuntimeError('이미 켜진 토크를 해제한 후 시작하세요.')
        # A calibration readback alone cannot confirm the live control mode.
        for name in JOINTS:
            mode=self.read(bus,name,'Operating_Mode')
            if mode!=0:raise RuntimeError(f'{name}: 위치 제어 모드가 아닙니다 (모드 {mode}).')
        self.transition('ACTIVATING','팔을 받친 상태로 현재 자세 확인')
        self.motion_attempted=True
        # Test the known problematic wrist before powering the load-bearing joints.
        for name in START_ORDER:
            self.check_abort();self.check_enabled(bus)
            if self.read(bus,name,'Torque_Enable')!=0 or self.read(bus,name,'Status') not in (0,1):raise RuntimeError(name+': 시작 전 상태 이상')
            self.temperature.observe(name,self.read(bus,name,'Present_Temperature'),time.monotonic())
            current=self.read(bus,name,'Present_Position')
            initial[name]=self.calibration.ticks({**initial,name:current})[name]
            for reg,value in [('Acceleration',0),('Goal_Time',0),('Goal_Velocity',1)]:
                self.write(bus,name,reg,value)
                if self.read(bus,name,reg)!=value or self.read(bus,name,'Torque_Enable')!=0:raise RuntimeError(name+': 시작 설정 확인 실패')
            self.activation_sample(bus,name,initial[name],'before_goal_write')
            self.write(bus,name,'Goal_Position',initial[name])
            written=self.activation_sample(bus,name,initial[name],'after_goal_write')
            implicit=written['torque']
            if implicit not in (0,1):raise RuntimeError(name+': 토크 상태 읽기 오류')
            if implicit:self.publish('notice',f'{name}: 실물 {initial[name]}틱을 자동 유지 목표로 기록 · 토크 ON 확인 · 실물 위치/내부 목표 대조 중')
            if self.read(bus,name,'Goal_Position')!=initial[name]:raise RuntimeError(name+': 시작 목표 읽기 불일치')
            if not implicit:self.write(bus,name,'Torque_Enable',1)
            self.enabled[name]=initial[name]
            until=time.monotonic()+.35
            while time.monotonic()<until:self.check_enabled(bus);self.stop.wait(.01)
        self.last_goals=initial.copy();self.transition('HOLD','현재 자세 유지')
    def check_active(self,snapshot):
        self.check_abort()
        if not snapshot.fresh() or not snapshot.calibration_matches:raise RuntimeError('실행 중 위치/영점 확인 실패')
        for n,h in snapshot.telemetry.items():
            path=self.internal_paths.get(n);transition=bool(path and time.monotonic()<=path[2])
            if 'internal_goal_ticks' in h:
                start=self.segment[0][n] if self.state=='MOVING' and self.segment else snapshot.ticks[n] if self.state=='FOLLOW' else self.last_goals[n]
                goal=self.segment[1][n] if self.state=='MOVING' and self.segment else self.last_goals[n]
                if n in self.load_compensation.states:start,goal=min(start,goal,self.last_goals[n]),max(start,goal,self.last_goals[n])
                if transition:start,goal=path[:2]
                self.check_internal_target(n,h['internal_goal_ticks'],start,goal,actual=snapshot.ticks[n],command_goal=h.get('goal_ticks'))
            if h['torque']!=1 or h['status'] not in (0,1):raise RuntimeError(n+': 토크·모터 상태 이상')
            if h['status']==1 or h.get('packet_alarm',0)==1:self.observe_voltage(n,True)
            self.temperature.observe(n,h['temperature_c'],time.monotonic())
            if h['goal_ticks']!=self.last_goals[n]:
                if self.state=='FOLLOW' and self.calibration.motors[n].low<=h['goal_ticks']<=self.calibration.motors[n].high:
                    since=self.sync_unconfirmed.setdefault(n,time.monotonic())
                    if time.monotonic()-since>.3:return n+': 동기 목표 수신 확인 지연 · 현재 자세 유지'
                else:raise RuntimeError(n+': 모터 목표 레지스터가 변경되었습니다.')
            else:self.sync_unconfirmed.pop(n,None)
            error=abs(snapshot.ticks[n]-self.last_goals[n])
            if error>RUNAWAY_TICKS:
                if n=='gripper' and self.grip_contact.candidate(snapshot.ticks[n],self.requested_grip(),h):pass
                elif transition and min(path[:2])-RUNAWAY_TICKS<=snapshot.ticks[n]<=max(path[:2])+RUNAWAY_TICKS:pass
                else:raise RuntimeError(n+f': 목표에서 {RUNAWAY_TICKS}틱 이상 크게 이탈했습니다.')
            if self.state=='FOLLOW' and error>RUNAWAY_TICKS and not (n=='gripper' and self.grip_contact.candidate(snapshot.ticks[n],self.requested_grip(),h)):
                prior=self.follow_progress.get(n)
                if prior is None or abs(snapshot.ticks[n]-prior[0])>=4:self.follow_progress[n]=(snapshot.ticks[n],time.monotonic())
                elif time.monotonic()-prior[1]>1.5:return n+': 따라가기 중 위치 진행 없음 · 현재 자세 유지'
            else:self.follow_progress.pop(n,None)
    def note_goals(self,ticks):
        for n,v in ticks.items():
            if self.last_goals.get(n)==v:continue
            old=self.last_goals.get(n,v);h=self.last_snapshot.telemetry.get(n,{}) if self.last_snapshot else {}
            internal=h.get('internal_goal_ticks',old)
            low,high=min(old,internal,v),max(old,internal,v)
            self.internal_paths[n]=(low,high,time.monotonic()+max(.5,(high-low)/getattr(self,'active_rate',self.rate_ticks_s)+.3))
    def send(self,bus,ticks,*,sync=False):
        ticks={**ticks,'gripper':self.grip_contact.constrain(ticks['gripper'])}
        try:ticks=self.calibration.ticks(ticks)
        except ValueError as exc:raise RuntimeError('명령 틱 범위 오류: '+str(exc)) from exc
        self.note_goals(ticks)
        if sync:
            self.check_abort();bus.motion_gate.allow_sync_goals(ticks)
            record={'at':time.time(),'register':'Goal_Position','sync':True,'values':ticks.copy(),'transport_sent':False};self.command_log.append(record)
            try:bus.sync_write('Goal_Position',ticks,normalize=False,num_retry=0)
            finally:bus.motion_gate.permit=None
            record['transport_sent']=True;self.last_goals=ticks.copy();return
        for n,v in ticks.items():
            if self.last_goals.get(n)!=v:
                self.write(bus,n,'Goal_Position',v);self.last_goals[n]=v
    def begin_segment(self,now,*,continuous=False):
        self.residual_reason=None;self.residual_since=None
        self.load_compensation.reset()
        if continuous and self.joint_path and self.path_slot+1<len(self.joint_path.segments):
            self.path_slot+=1
        else:
            # Re-anchor after a real stop/contact correction. Never carry a stale bias
            # into a newly planned segment as a physical target.
            targets=[{**t,'gripper':self.grip_contact.constrain(t['gripper'],limit_pending=False)} for t in self.sequence[self.index:]]
            self.joint_path=JointPath(self.last_goals,targets,self.rate_ticks_s,first_stop=self.index==0,
                stop_flags=self.sequence_stops[self.index:] or None)
            self.path_slot=0
        self.path_segment=self.joint_path.segments[self.path_slot]
        start=self.path_segment.start.copy();goal=self.path_segment.goal.copy()
        goal['gripper']=self.grip_contact.constrain(goal['gripper'],opening_intent=True,limit_pending=False)
        self.segment=(start,goal,now,self.path_segment.duration);self.settled_at=None
    def requested_grip(self):
        return self.segment[1]['gripper'] if self.state=='MOVING' and self.segment else self.last_goals['gripper']
    def protect_gripper(self,bus,snapshot):
        event=self.grip_contact.observe(snapshot.ticks['gripper'],self.requested_grip(),snapshot.telemetry['gripper'],now=snapshot.monotonic)
        if event is None:
            # During confirmation, keep the already-sent closing goal.
            # Do not increase pressure or turn our pressure reduction into evidence.
            if self.grip_contact.pending is not None:
                self.send(bus,self.last_goals.copy())
            return
        event['at']=time.time();self.report.setdefault('gripper_contacts',[]).append(event)
        event['rate_ticks_s']=getattr(self,'active_rate',self.rate_ticks_s)
        self.report['gripper_contacts']=self.report['gripper_contacts'][-100:]
        # Replace the servo target immediately, not merely the completion condition.
        self.send(bus,{**self.last_goals,'gripper':event['hold_tick']})
        if self.state=='MOVING':self.begin_segment(time.monotonic())
        self.publish('notice',f'집게 부하 감지 · {event["hold_tick"]}틱에서 접촉 유지 · 더 닫지 않습니다.')
    def on_snapshot(self,bus,snapshot):
        try:
            self.last_snapshot=snapshot
            if self.release_requested.is_set():
                self.motion_attempted=True;self.release(bus);return
            if self.state in ('HOLD','MOVING','FOLLOW'):
                pause=self.check_active(snapshot)
                if pause:self.pause_motion(bus,snapshot,pause);return
                self.protect_gripper(bus,snapshot)
                if self.hold_requested.is_set():
                    self.send(bus,snapshot.ticks);self.load_compensation.reset();self.sequence=[];self.segment=None;self.hold_requested.clear();self.program_active.clear();self.command_pending.clear()
                    while not self.commands.empty():
                        try:self.commands.get_nowait()
                        except queue.Empty:break
                    self.transition('HOLD','이동 정지 · 현재 자세 유지');return
            else:self.hold_requested.clear()
            try:action,values,submitted,request_id=self.commands.get_nowait()
            except queue.Empty:action=None
            if action:
                self.command_pending.clear();self.active_request_id=request_id
                if time.monotonic()-submitted>5:raise ValueError('5초 이상 대기한 명령을 취소했습니다. 다시 실행하세요.')
                if action=='arm':
                    if self.state!='READ_ONLY':raise ValueError('이미 토크를 유지하고 있습니다.')
                    self.arm(bus,snapshot);return
                if self.state!='HOLD':raise ValueError('현재 자세 유지가 확인된 후 실행하세요.')
                self.configure_motion_speed(bus,follow=action=='follow')
                if action=='follow':
                    self.load_compensation.reset()
                    if self.leader_provider is None:raise ValueError('리더암을 먼저 연결하세요.')
                    self.send(bus,snapshot.ticks)
                    self.follow_initial=True;self.follow_initial_target={n:float(v) for n,v in snapshot.ticks.items()}
                    self.report['follow_start_rate_ticks_s']=self.follow_start_rate
                    self.follow_time=time.monotonic();self.follow_last_valid_at=self.follow_time;self.follow_waiting=False;self.follow_recovering=False;self.follow_control_at=self.follow_time;self.follow_limit_key=();self.follow_progress={};self.sync_unconfirmed={};self.transition('FOLLOW','리더 위치로 천천히 합류 중');return
                self.episode_run=action=='play';self.sequence=values;self.sequence_stops=[s.stop for s in JointPath(self.last_goals,values,self.rate_ticks_s).segments];self.index=0;self.begin_segment(time.monotonic());self.transition('MOVING',f'스텝 1 / {len(values)}')
            if self.state=='FOLLOW':
                leader=self.leader_provider();now=time.monotonic()
                if now-self.heartbeat>HEARTBEAT_TIMEOUT:self.pause_motion(bus,snapshot,'화면 응답 지연 · 따라가기 정지');return
                if leader is not None and (leader.role!='leader' or not leader.calibration_matches):
                    self.pause_motion(bus,snapshot,'리더 영점 확인 실패 · 따라가기 정지');return
                if leader is None or not leader.fresh(now):
                    if not self.follow_waiting:
                        self.follow_waiting=True;self.follow_recovering=True
                        self.send(bus,snapshot.ticks)
                        if self.follow_initial:self.follow_initial_target={n:float(v) for n,v in snapshot.ticks.items()}
                        self.publish('notice','리더 수신 지연 · 현재 자세에서 대기 (2초 이내 복구 시 계속)')
                    self.follow_control_at=now
                    last=self.follow_last_valid_at
                    if leader is not None:last=max(last, min(now,leader.monotonic))
                    if now-last>=FOLLOW_RECEIVE_TIMEOUT:
                        self.pause_motion(bus,snapshot,'리더 수신 2초 중단 · 현재 자세 유지 · 따라가기를 다시 시작하세요.')
                    return
                self.follow_last_valid_at=leader.monotonic
                if self.follow_waiting:
                    self.follow_waiting=False;self.publish('notice','리더 수신 복구 · 속도를 제한해 따라가기 계속')
                try:desired,limited=leader_target(leader.ticks,self.leader_calibration,self.calibration,with_limits=True)
                except ValueError as exc:self.pause_motion(bus,snapshot,str(exc));return
                limit_key=tuple((n,v['applied']) for n,v in limited.items())
                if limit_key!=getattr(self,'follow_limit_key',()):
                    self.follow_limit_key=limit_key
                    self.publish('notice','리더 따라가기 · '+('범위 안에서 제한: '+', '.join(f'{LABELS[JOINTS.index(n)]} {v["applied"]}틱' for n,v in limited.items()) if limited else '관절 제한 해제'))
                desired['gripper']=self.grip_contact.constrain(desired['gripper'],opening_intent=True)
                if self.follow_initial:
                    distance=self.follow_start_rate*min(.05,max(0.,now-self.follow_control_at))
                    self.follow_initial_target={n:max(self.follow_initial_target[n]-distance,min(self.follow_initial_target[n]+distance,v)) for n,v in desired.items()}
                    limited_target={n:round(v) for n,v in self.follow_initial_target.items()}
                    if limited_target==desired and all(abs(snapshot.ticks[n]-desired[n])<=BODY_ARRIVAL_TOLERANCE_TICKS for n in JOINTS):
                        self.follow_initial=False;self.follow_recovering=False
                        self.publish('notice','최초 합류 완료 · 기존 속도로 리더 따라가기')
                    desired=limited_target
                elif self.follow_recovering:
                    # Catch up from the held target gradually; never replay queued poses.
                    distance=max(1,int(FOLLOW_RATE_TICKS_S*min(.05,max(0.,now-self.follow_control_at))))
                    limited_target={n:max(self.last_goals[n]-distance,min(self.last_goals[n]+distance,v)) for n,v in desired.items()}
                    self.follow_recovering=limited_target!=desired;desired=limited_target
                self.follow_control_at=now
                self.send(bus,desired,sync=True)
            if self.state=='MOVING':
                if time.monotonic()-self.heartbeat>HEARTBEAT_TIMEOUT:
                    self.pause_motion(bus,snapshot,'화면 응답 2초 지연 · 이동만 정지, 토크 유지');return
                start,goal,began,duration=self.segment;now=time.monotonic();fraction=min(1.,(now-began)/duration)
                target=self.path_segment.sample(fraction)
                passing=bool(fraction>=1 and not self.path_segment.stop and not self.load_compensation.states and self.grip_contact.pending is None
                             and all(abs(snapshot.ticks[n]-goal[n])<=PASS_THROUGH_TOLERANCE_TICKS for n in JOINTS))
                # A delayed worker cannot jump ahead in the trajectory.
                if not self.load_compensation.states and any(abs(target[n]-self.last_goals[n])>self.max_step_ticks for n in JOINTS):
                    self.begin_segment(now);return
                try:
                    command=self.last_goals.copy() if self.residual_reason else self.load_compensation.command(target,snapshot,self.calibration,now,enabled=fraction>=1 and not passing,
                        prepare_goal=goal if fraction>=.8 and self.path_segment.stop else None)
                except CorrectionStalled as exc:
                    if not self.can_continue_residual(snapshot,goal,now):
                        self.pause_motion(bus,snapshot,str(exc));return
                    self.accept_residual(str(exc),now);command=self.last_goals.copy()
                except ValueError as exc:
                    self.pause_motion(bus,snapshot,str(exc));return
                if self.load_compensation.states:
                    if not self.report.get('load_compensation_active'):
                        self.publish('notice','어깨·팔꿈치 부하 오차 보정 중 · 티칭 목표 유지')
                    self.report['load_compensation_active']=True
                    self.report['load_compensation']=self.load_compensation.evidence()
                self.send(bus,command)
                tolerances={n:self.load_compensation.tolerance(n,ARRIVAL_TOLERANCE_TICKS if n=='gripper' else BODY_ARRIVAL_TOLERANCE_TICKS) for n in JOINTS}
                if self.residual_reason:tolerances.update({n:EPISODE_RESIDUAL_LIMIT_TICKS for n in JOINTS if n!='gripper'})
                outside={n:snapshot.ticks[n]-goal[n] for n in JOINTS if abs(snapshot.ticks[n]-goal[n])>tolerances[n]}
                residual_ready=not self.residual_reason or self.can_continue_residual(snapshot,goal,now)
                if fraction>=1 and (passing or not outside) and self.grip_contact.pending is None and residual_ready:
                    self.settled_at=self.settled_at or now
                    if passing or now-self.settled_at>=.2:
                        completion={'at':time.time(),'request_id':self.active_request_id,'step_index':self.index+1,
                                    'arrival_type':'residual_accepted' if self.residual_reason else 'pass_through' if passing else 'settled',
                                    'residual_acceptance_reason':self.residual_reason,
                                    'tolerance_ticks':dict.fromkeys(JOINTS,PASS_THROUGH_TOLERANCE_TICKS) if passing else tolerances.copy(),
                                    'target':goal.copy(),'actual':snapshot.ticks.copy(),'servo_command':self.last_goals.copy(),
                                    'residual_ticks':{n:snapshot.ticks[n]-goal[n] for n in JOINTS},
                                    'load_compensation':self.load_compensation.evidence()}
                        self.report.setdefault('step_arrivals',[]).append(completion)
                        self.report['step_arrivals']=self.report['step_arrivals'][-200:]
                        if self.audit_path:
                            try:atomic_json(self.audit_path.parent/f'arrival-{self.audit_path.stem}.json',
                                            {'steps':self.report['step_arrivals']})
                            except OSError as exc:self.report['arrival_record_error']=str(exc)
                        if self.residual_reason:self.publish('notice',f'스텝 {self.index+1}: 작은 잔여 오차 기록 후 진행 · '+', '.join(f'{LABELS[JOINTS.index(n)]} {v:+d}틱' for n,v in completion['residual_ticks'].items() if v))
                        self.index+=1
                        if self.index==len(self.sequence):self.completed_request_id=self.active_request_id;self.sequence=[];self.segment=None;self.program_active.clear();self.transition('HOLD','실물 스텝 실행 완료')
                        else:self.begin_segment(began+duration if passing else now,continuous=passing);self.transition('MOVING',f'스텝 {self.index+1} / {len(self.sequence)}')
                else:
                    self.settled_at=None
                    if now-began>duration+5:
                        if not self.residual_reason and self.can_continue_residual(snapshot,goal,now):
                            self.accept_residual('도착 대기 후 작은 잔여 오차',now);return
                        if self.residual_reason and now-self.residual_since<.5:return
                        differences=', '.join(f'{LABELS[JOINTS.index(n)]} {delta:+d}틱 (목표 {goal[n]} / 실제 {snapshot.ticks[n]})' for n,delta in outside.items())
                        if not differences:
                            differences=('집게 접촉 확인 중' if self.grip_contact.pending is not None else '도착 안정 확인 중')
                        self.pause_motion(bus,snapshot,f'스텝 {self.index+1}/{len(self.sequence)} · 목표 도달 5초 지연: {differences} · 자세 유지');return
        except ValueError as exc:
            self.command_pending.clear();self.program_active.clear()
            if self.state in ('ACTIVATING','MOVING'):
                self.transition('FAULT',str(exc));raise
            self.publish('notice',str(exc))
        except Exception:
            self.program_active.clear();self.command_pending.clear();self.transition('FAULT','실물 제어 중단');raise
    def can_continue_residual(self,snapshot,goal,now):
        # Episode progress is distinct from precision arrival. Never use this for
        # stale data, a gripper still closing, a large error or a health failure.
        if (not self.episode_run or not snapshot.fresh(now) or not snapshot.calibration_matches
                or self.grip_contact.pending is not None):return False
        for n in JOINTS:
            h=snapshot.telemetry.get(n,{})
            limit=ARRIVAL_TOLERANCE_TICKS if n=='gripper' else EPISODE_RESIDUAL_LIMIT_TICKS
            raw=h.get('load_raw');velocity=h.get('velocity_signed_raw')
            if (abs(snapshot.ticks[n]-goal[n])>limit or h.get('torque')!=1
                    or h.get('status')!=0 or h.get('packet_alarm',0)!=0
                    or h.get('temperature_c',100)>=60 or type(raw) is not int
                    or not 0<=raw<=2047 or (raw&1023)>MAX_LOAD
                    or velocity is None or abs(velocity)>5):return False
        return True
    def accept_residual(self,reason,now):
        self.residual_reason=reason;self.residual_since=now;self.settled_at=None
    def pause_motion(self,bus,snapshot,detail):
        evidence={'at':time.time(),'detail':detail,'request_id':self.active_request_id,'step_index':self.index+1 if self.sequence else None,
                  'target':self.segment[1].copy() if self.segment else self.last_goals.copy(),'actual':snapshot.ticks.copy(),
                  'telemetry':snapshot.telemetry,'gripper_hold_tick':self.grip_contact.hold_tick,
                  'load_compensation':self.load_compensation.evidence(),
                  'servo_command':self.last_goals.copy(),
                  'gripper_contact_threshold':self.grip_contact.threshold,
                  'gripper_contact_pending':dict(self.grip_contact.pending) if self.grip_contact.pending else None}
        self.report.setdefault('pauses',[]).append(evidence);self.report['pauses']=self.report['pauses'][-30:]
        self.send(bus,snapshot.ticks)
        self.load_compensation.reset()
        self.sequence=[];self.segment=None;self.program_active.clear();self.command_pending.clear()
        self.transition('HOLD',detail,paused=True)
        if self.audit_path:
            try:atomic_json(self.audit_path.parent/f'stop-{time.time_ns()}.json',evidence)
            except OSError as exc:self.report['stop_record_error']=str(exc)
    def before_close(self,bus):
        self.report['motion_events']=self.motion_log
        if self.motion_attempted:
            fault=self.state=='FAULT' or self.error is not None
            self.release(bus)
            if fault:self.transition('FAULT','제어 중단 · 토크 OFF 확인')
        self.program_active.clear();self.command_pending.clear()
        self.report['command_trace']=list(self.command_log)
