"""Limited gravity-informed assistance in the leader's existing position mode.

The model predicts gravity, not a calibrated motor torque command. A small
position bias and a low SRAM torque limit provide partial assistance. No motor
mode, PID, calibration, limits, or EEPROM settings are written.
"""
from collections import deque
from copy import deepcopy
import math
import threading
import time
import xml.etree.ElementTree as ET
import numpy as np
from .devices import ReadOnlySession,ReadOnlyViolation,new_bus
from .domain import ROOT,JOINTS,atomic_json
from .motion import MotionGate
from .communication import CommunicationCancelled

TARGETS=('shoulder_lift','elbow_flex')
# These are conservative trial levels, not percentages of gravity cancellation.
LEVELS={'약하게':{'gain':.25,'offset':12,'limit':60},
        '보통':{'gain':.5,'offset':24,'limit':100}}
REGISTERS={'Torque_Enable':(40,1),'Acceleration':(41,1),'Goal_Position':(42,2),
           'Goal_Time':(44,2),'Goal_Velocity':(46,2),'Torque_Limit':(48,2)}
RESTORED=('Acceleration','Goal_Time','Goal_Velocity','Torque_Limit')
MAX_AGE=.2


class MotionEnvelope:
    """Continuous speed weighting with filtered, slower recovery of assistance.

    The scale is a controller setting, not a percentage of measured hand force.
    A small motion floor retains support; normal movement never toggles torque.
    """
    MIN_SCALE=.30
    FULL_SPEED=3.       # deg/s: keep full support for slow deliberate movement
    LIGHT_SPEED=20.     # deg/s: reach the motion floor at normal teaching speed
    SPEED_TAU=.10
    REDUCE_TAU=.10
    RECOVER_TAU=.50
    OUTPUT_TAU=.15
    MAX_REDUCE_RATE=1.5
    MAX_RECOVER_RATE=.5
    def __init__(self):
        self.speed=0.;self.target_stage=self.MIN_SCALE;self.scale=self.MIN_SCALE
    def update(self,speed_deg_s,dt):
        if not math.isfinite(speed_deg_s) or not math.isfinite(dt) or dt<0:raise ValueError('리더 보조 속도·시간 오류')
        dt=min(dt,.05)
        if dt==0:return self.scale
        self.speed+=(abs(speed_deg_s)-self.speed)*(-math.expm1(-dt/self.SPEED_TAU))
        u=max(0.,min(1.,(self.speed-self.FULL_SPEED)/(self.LIGHT_SPEED-self.FULL_SPEED)))
        smooth=u*u*u*(10.+u*(-15.+6.*u))
        target=1.-(1.-self.MIN_SCALE)*smooth
        tau=self.REDUCE_TAU if target<self.target_stage else self.RECOVER_TAU
        self.target_stage+=(target-self.target_stage)*(-math.expm1(-dt/tau))
        change=(self.target_stage-self.scale)*(-math.expm1(-dt/self.OUTPUT_TAU))
        self.scale+=max(-self.MAX_REDUCE_RATE*dt,min(self.MAX_RECOVER_RATE*dt,change))
        self.scale=max(self.MIN_SCALE,min(1.,self.scale))
        return self.scale


class AssistCancelled(RuntimeError):pass


class GravityModel:
    """Use the bundled SO-101 inertias as a nominal estimate, without graphics."""
    def __init__(self,offsets=None):
        import mujoco
        self.mujoco=mujoco
        tree=ET.parse(ROOT/'assets/so101/so101_modified.xml').getroot()
        for asset in list(tree.findall('asset')):tree.remove(asset)
        for parent in tree.iter():
            for child in list(parent):
                if child.tag in ('geom','site','camera','light'):parent.remove(child)
        self.model=mujoco.MjModel.from_xml_string(ET.tostring(tree,encoding='unicode'))
        self.data=mujoco.MjData(self.model)
        self.offsets={n:float((offsets or {}).get(n,math.pi/2 if n=='gripper' else 0.)) for n in JOINTS}
        if not all(math.isfinite(v) for v in self.offsets.values()):raise ValueError('리더 중력 모델 기준각 오류')
        self.qpos={n:int(self.model.joint(n).qposadr[0]) for n in JOINTS}
        self.dof={n:int(self.model.joint(n).dofadr[0]) for n in TARGETS}
    def torques(self,ticks,calibration):
        mapping=calibration.angle_mapping
        if mapping is None:raise ValueError('리더 무게 보조에는 리더의 3점 각도 보정이 필요합니다.')
        calibration.ticks(ticks)
        for n in JOINTS:self.data.qpos[self.qpos[n]]=self.offsets[n]+math.radians(mapping.degrees(n,ticks[n]))
        self.data.qvel[:]=0;self.data.qacc[:]=0
        self.mujoco.mj_forward(self.model,self.data)
        out={n:float(self.data.qfrc_bias[i]) for n,i in self.dof.items()}
        if not all(math.isfinite(v) and abs(v)<3 for v in out.values()):raise ValueError('리더 중력 계산 범위 오류')
        return out


class AssistGate(MotionGate):
    """Single-use exact packets, restricted to two joints and SRAM registers."""
    def __init__(self,calibration):
        super().__init__(calibration);self.original={}
    def allow(self,name,register,value,*,restore=False):
        if name not in TARGETS or register not in REGISTERS or type(value) is not int:
            raise ReadOnlyViolation('리더 어깨·팔꿈치의 허용된 RAM 명령만 사용할 수 있습니다.')
        address,length=REGISTERS[register]
        m=self.calibration.motors[name]
        if restore:
            legal=register in RESTORED and self.original.get(name,{}).get(register)==value
        else:
            legal={'Torque_Enable':value in (0,1),'Acceleration':value==0,'Goal_Time':value==0,
                   'Goal_Velocity':value in (20,1000),'Torque_Limit':1<=value<=100,
                   'Goal_Position':m.low+8<=value<=m.high-8}[register]
        if not legal or not 0<=value<2**(length*8):raise ReadOnlyViolation('리더 보조 명령 범위 오류')
        self.permit=(m.id,address,value.to_bytes(length,'little'))
    def allow_sync_goals(self,ticks):raise ReadOnlyViolation('리더 보조의 동기 쓰기는 허용하지 않습니다.')


def assist_bus(port,calibration,counts):
    gate=AssistGate(calibration);bus=new_bus(port,calibration,counts,gate_installer=gate.install)
    bus.assist_gate=gate;return bus


class LeaderAssistSession(ReadOnlySession):
    poll_seconds=.02
    def __init__(self,port,calibration,**kwargs):
        if kwargs.get('role','leader')!='leader':raise ValueError('리더 전용 보조 연결입니다.')
        kwargs['role']='leader';factory=kwargs.pop('bus_factory',assist_bus)
        super().__init__(port,calibration,bus_factory=factory,**kwargs)
        self.requires_port_check=factory is assist_bus
        self.assist_state='OFF';self.assist_error=None;self.assist_level=None;self.assist_interruption=None
        self.assist_lock=threading.Lock();self.assist_request=None;self.assist_stop=threading.Event()
        self.touched=[];self.original={};self.last_goals={};self.bias={n:0. for n in TARGETS}
        self.model=None;self.control_at=None;self.segments={};self.output_limits={};self.limit_checked_at=0.;self.command_log=deque(maxlen=600)
        self.base_output_limits={};self.envelopes={};self.speed_ticks={};self.speed_at=None
        self.adaptation_history=deque(maxlen=600);self.adaptation_at=0.
    def begin_assist(self,level,guard,offsets=None):
        if level not in LEVELS:raise ValueError('리더 보조 강도를 확인하세요.')
        if self.calibration.angle_mapping is None:raise ValueError('리더의 3점 각도 보정 후 무게 보조를 사용할 수 있습니다.')
        if not self.running or not self.latest or not self.latest.fresh() or not self.latest.calibration_matches:
            raise ValueError('리더 현재값·영점 확인 후 무게 보조를 시작하세요.')
        if self.assist_error:raise ValueError('리더 보조 오류를 확인하고 리더를 다시 연결하세요: '+self.assist_error)
        with self.assist_lock:
            if self.assist_request or self.touched:raise ValueError('리더 보조가 이미 준비·실행 중입니다.')
            self.assist_stop.clear();self.assist_request=(level,guard,deepcopy(offsets),time.monotonic()+2.)
            self.assist_state='WAITING';self.assist_level=level;self.model=None;self.assist_interruption=None
    def stop_assist(self):self.assist_stop.set()
    def _read(self,bus,n,reg):return self.communication.retry_read(bus,n+' '+reg,lambda:bus.read(reg,n,normalize=False,num_retry=0))
    def _write(self,bus,n,reg,value,*,restore=False):
        def send():
            if not restore and not (reg=='Torque_Enable' and value==0):
                if self.assist_stop.is_set() or not self.assist_request or self.assist_request[1]()!='RUN':raise AssistCancelled('리더 보조 중단 요청')
            bus.assist_gate.allow(n,reg,int(value),restore=restore)
            try:bus.write(reg,n,int(value),normalize=False,num_retry=0)
            finally:bus.assist_gate.permit=None
        self.communication.verified_write(bus,n+' '+reg,int(value),send,lambda:bus.read(reg,n,normalize=False,num_retry=0),off=reg=='Torque_Enable' and value==0)
        self.command_log.append({'at':time.monotonic(),'joint':n,'register':reg,'value':int(value),'restore':restore})
    def _save_original(self):
        # Save before the first possible activation, including implicit ON at
        # Goal_Position on some firmware. Never overwrite the calibration file.
        self.report['leader_assist']={'kind':'position_bias','nominal_model':True,'level':self.assist_level,'before':deepcopy(self.original),
                                     'motion_adaptation':{'minimum_scale':MotionEnvelope.MIN_SCALE,'full_support_deg_s':MotionEnvelope.FULL_SPEED,
                                                          'light_support_deg_s':MotionEnvelope.LIGHT_SPEED,'speed_filter_s':MotionEnvelope.SPEED_TAU,
                                                          'reduction_filter_s':MotionEnvelope.REDUCE_TAU,'recovery_filter_s':MotionEnvelope.RECOVER_TAU,
                                                          'output_filter_s':MotionEnvelope.OUTPUT_TAU}}
        if self.audit_path:atomic_json(self.audit_path,self.report)
    def _health(self,snapshot,now):
        if (snapshot.role!='leader' or snapshot.calibration_sha256!=self.calibration.sha256
                or not snapshot.calibration_matches or not 0<=now-snapshot.monotonic<=MAX_AGE):
            raise RuntimeError('리더 보조의 최신 위치·영점 확인 실패')
        self.calibration.ticks(snapshot.ticks)
        for n in TARGETS:
            h=snapshot.telemetry[n];m=self.calibration.motors[n]
            if h.get('status')!=0 or h.get('packet_alarm',0)!=0:raise RuntimeError(n+': 리더 보조 중 서보 경고')
            if not 0<=h.get('temperature_c',100)<55:raise RuntimeError(n+': 리더 보조 온도 범위 초과')
            if not m.low+32<=snapshot.ticks[n]<=m.high-32:raise RuntimeError(n+': 관절 끝 범위에 가까워 리더 보조를 해제합니다.')
    def _activate(self,bus,snapshot,request):
        level,guard,offsets,_=request;self._health(snapshot,time.monotonic())
        if any(h.get('torque')!=0 for h in snapshot.telemetry.values()):raise RuntimeError('리더 토크 OFF 상태에서만 보조를 시작할 수 있습니다.')
        self.model.torques(snapshot.ticks,self.calibration)
        before={n:{r:self._read(bus,n,r) for r in (*RESTORED,'Operating_Mode','I_Coefficient','P_Coefficient','Torque_Enable','Max_Torque_Limit','Phase')} for n in TARGETS}
        for n,row in before.items():
            if row['Operating_Mode']!=0 or row['Torque_Enable']!=0 or row['I_Coefficient']!=0 or not 1<=row['P_Coefficient']<=64:
                raise RuntimeError(n+': 위치 모드·토크 OFF·I=0·P 범위 확인 실패')
            if not 1<=row['Torque_Limit']<=1000 or not 1<=row['Max_Torque_Limit']<=1000:raise RuntimeError(n+': 기존 출력 상한 확인 실패')
        self.original=before;bus.assist_gate.original=deepcopy(before);self._save_original()
        self.envelopes={n:MotionEnvelope() for n in TARGETS};self.adaptation_history.clear();self.adaptation_at=0.
        self.assist_state='STARTING'
        for n in TARGETS:
            if self.assist_stop.is_set() or guard()!='RUN':raise AssistCancelled('리더 보조 시작 중 따라가기 중단')
            if self._read(bus,n,'Torque_Enable')!=0:raise RuntimeError(n+': 시작 전 리더 토크가 변경됐습니다.')
            current=self._read(bus,n,'Present_Position');m=self.calibration.motors[n]
            if abs(current-snapshot.ticks[n])>32 or not m.low+32<=current<=m.high-32:raise RuntimeError(n+': 리더 시작 자세가 변경됐습니다.')
            self.touched.append(n)
            self.base_output_limits[n]=min(LEVELS[level]['limit'],before[n]['Torque_Limit'],before[n]['Max_Torque_Limit'])
            cap=max(1,round(self.base_output_limits[n]*MotionEnvelope.MIN_SCALE));self.output_limits[n]=cap
            for reg,value in [('Torque_Limit',cap),('Acceleration',0),('Goal_Time',0),('Goal_Velocity',1000 if before[n]['Phase']&4 else 20),('Goal_Position',current)]:
                self._write(bus,n,reg,value)
                if self._read(bus,n,reg)!=value:raise RuntimeError(n+': 리더 보조 설정 읽기 불일치 ('+reg+')')
                if reg!='Goal_Position' and self._read(bus,n,'Torque_Enable')!=0:raise RuntimeError(n+': 시작 설정 중 예상하지 못한 토크 ON')
            # Changing a goal can implicitly enable torque on this hardware.
            if self._read(bus,n,'Torque_Enable')==0:self._write(bus,n,'Torque_Enable',1)
            if self._read(bus,n,'Torque_Enable')!=1:raise RuntimeError(n+': 리더 토크 ON 확인 실패')
            actual=self._read(bus,n,'Present_Position');internal=self._read(bus,n,'Internal_Goal_Position')
            if abs(actual-current)>24 or abs(internal-current)>8:raise RuntimeError(n+': 리더 시작 목표 이탈')
            self.last_goals[n]=current;self.segments[n]=(current,current)
        self.bias=dict.fromkeys(TARGETS,0.);self.control_at=time.monotonic();self.limit_checked_at=self.control_at;self.assist_state='ACTIVE'
        self.speed_ticks={n:snapshot.ticks[n] for n in TARGETS};self.speed_at=snapshot.monotonic
        self.publish('notice','리더 무게 보조 · '+level+' · 움직임에 맞춰 부드럽게 조절')
    def _command(self,bus,snapshot,request):
        now=time.monotonic();self._health(snapshot,now)
        if self.control_at is None or not 0<=now-self.control_at<=MAX_AGE:raise RuntimeError('리더 보조 제어 주기 지연')
        dt=min(.05,now-self.control_at);self.control_at=now
        if now-self.limit_checked_at>=.5:
            for n in TARGETS:
                if self._read(bus,n,'Torque_Limit')!=self.output_limits[n] or self._read(bus,n,'Operating_Mode')!=0:
                    raise RuntimeError(n+': 리더 출력 상한·모드가 변경됐습니다.')
            self.limit_checked_at=now
        level=LEVELS[request[0]];torques=self.model.torques(snapshot.ticks,self.calibration)
        measured_dt=snapshot.monotonic-self.speed_at
        if measured_dt<=0:raise RuntimeError('리더 보조 위치의 측정 시간이 진행되지 않았습니다.')
        scales={};speeds={};caps={}
        for n in TARGETS:
            h=snapshot.telemetry[n]
            if h.get('torque')!=1 or h.get('goal_ticks')!=self.last_goals[n]:raise RuntimeError(n+': 리더 보조 상태·목표가 변경됐습니다.')
            internal=h.get('internal_goal_ticks')
            if internal is None or not min(self.segments[n])-8<=internal<=max(self.segments[n])+8:raise RuntimeError(n+': 리더 서보 내부 목표 이탈')
            if abs(h.get('velocity_signed_raw',0))>1400:raise RuntimeError(n+': 리더 이동 속도가 보조 범위를 넘었습니다.')
            # Nominal gain only: STS3215 has no calibrated effort interface here.
            # Keep the requested bias small and independent of the follower load.
            previous=self.speed_ticks[n];current=snapshot.ticks[n]
            # Ignore a single encoder count of stationary dither. For actual
            # motion use the measured angle mapping, independent of speed-register units.
            speed=0. if abs(current-previous)<=1 else abs(self.calibration.angle_mapping.degrees(n,current)-self.calibration.angle_mapping.degrees(n,previous))/measured_dt
            scale=self.envelopes[n].update(speed,dt);scales[n]=scale;speeds[n]=self.envelopes[n].speed
            cap=max(1,min(self.base_output_limits[n],round(self.base_output_limits[n]*scale)));caps[n]=cap
            bias=torques[n]*64*level['gain']*16/self.original[n]['P_Coefficient']
            bias=max(-level['offset'],min(level['offset'],bias))*scale
            self.bias[n]+=max(-24*dt,min(24*dt,bias-self.bias[n]))
            goal=round(snapshot.ticks[n]+self.bias[n]);m=self.calibration.motors[n]
            goal=max(m.low+8,min(m.high-8,goal))
            if request[1]()!='RUN' or self.assist_stop.is_set():raise AssistCancelled('리더 보조 중 따라가기 중단')
            # Reduce the cap before changing the goal; on recovery update the
            # goal first, so a higher cap never acts on an old position target.
            previous_cap=self.output_limits[n]
            if cap<previous_cap:self._set_output_limit(bus,n,cap)
            self.segments[n]=(snapshot.ticks[n],self.last_goals[n],goal)
            if goal!=self.last_goals[n]:self._write(bus,n,'Goal_Position',goal);self.last_goals[n]=goal
            if cap>previous_cap:self._set_output_limit(bus,n,cap)
        self.speed_ticks={n:snapshot.ticks[n] for n in TARGETS};self.speed_at=snapshot.monotonic
        self.report['leader_assist'].update(last_gravity_nm=torques,last_bias_ticks=dict(self.bias),
                                           last_motion_speed_deg_s=speeds,last_assist_scale=scales,last_output_limits=caps,state=self.assist_state)
        if now-self.adaptation_at>=.1:
            self.adaptation_history.append({'at':now,'wall_time':snapshot.wall_time,'speed_deg_s':speeds,'scale':scales,
                                            'torque_limit_raw':caps,'bias_ticks':dict(self.bias)})
            self.adaptation_at=now
    def _set_output_limit(self,bus,n,cap):
        if self._read(bus,n,'Torque_Limit')!=self.output_limits[n]:raise RuntimeError(n+': 리더 출력 상한이 변경됐습니다.')
        self._write(bus,n,'Torque_Limit',cap)
        if self._read(bus,n,'Torque_Limit')!=cap:raise RuntimeError(n+': 리더 출력 상한 변경 확인 실패')
        self.output_limits[n]=cap
    def _deactivate(self,bus):
        errors=[];remaining=[]
        with self.communication.cleanup():
            for n in reversed(self.touched):
                try:
                    self._write(bus,n,'Torque_Enable',0)
                    if self._read(bus,n,'Torque_Enable')!=0:raise RuntimeError('OFF 미확인')
                except Exception as exc:errors.append(n+': '+str(exc));remaining.append(n);continue
                # Do NOT restore a previous Goal_Position: it can energize the
                # motor again. Restore only bounded configuration with torque off.
                try:
                    for reg in RESTORED:
                        self._write(bus,n,reg,self.original[n][reg],restore=True)
                        if self._read(bus,n,reg)!=self.original[n][reg]:raise RuntimeError('RAM 설정 원복 읽기 불일치 ('+reg+')')
                        if self._read(bus,n,'Torque_Enable')!=0:raise RuntimeError('복원 후 OFF 미확인')
                except Exception as exc:errors.append(n+': '+str(exc));remaining.append(n)
        self.touched=remaining;self.control_at=None;self.last_goals={}
        self.assist_request=None;self.assist_state='FAULT' if self.assist_error or errors else 'OFF'
        if 'leader_assist' in self.report:self.report['leader_assist'].update(state=self.assist_state,restored=not errors,adaptation_history=list(self.adaptation_history))
        if errors:raise RuntimeError('리더 보조 해제 확인 실패: '+' / '.join(errors))
    def on_snapshot(self,bus,snapshot):
        with self.assist_lock:
            request=self.assist_request
            if self.assist_stop.is_set():
                self._deactivate(bus);return
            if request is None:return
            try:
                state=request[1]()
                if state=='DELAY':
                    # Release assistance at the existing freshness limit, but keep
                    # passive USB samples available to the follower's own watchdog.
                    self._deactivate(bus)
                    self.assist_interruption='화면·팔로워 수신 지연'
                    self.report.setdefault('assist_interruptions',[]).append({'at':time.time(),'reason':self.assist_interruption})
                    self.report['assist_interruptions']=self.report['assist_interruptions'][-30:]
                    self.publish('device_notice','수신 지연으로 리더 무게 보조 해제 · 리더 읽기 유지')
                    return
                if state=='LOST':raise RuntimeError('리더 보조의 화면·팔로워 통신이 지연됐습니다.')
                if state!='RUN':
                    if self.assist_state=='WAITING' and state=='WAIT' and time.monotonic()<=request[3]:return
                    self._deactivate(bus);return
                if self.assist_state=='WAITING':
                    if self.model is None:
                        self.model=GravityModel(request[2]);return  # Re-read a fresh pose after loading the model.
                    self._activate(bus,snapshot,request)
                else:self._command(bus,snapshot,request)
            except AssistCancelled:
                self._deactivate(bus)
            except CommunicationCancelled:
                self._deactivate(bus);raise
            except Exception as exc:
                self.assist_error=str(exc);self.report['leader_assist_error']=self.assist_error
                self._deactivate(bus)
                self.publish('error','리더 무게 보조 중단: '+self.assist_error)
                self.stop.set()
    def before_close(self,bus):
        with self.assist_lock:self._deactivate(bus)
