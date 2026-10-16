"""Fixed per-arm angle map for simulation preview and offline experiment replay."""
from copy import deepcopy
from pathlib import Path
import json
import hashlib
import numpy as np
from .domain import ROOT,JOINTS

def _interpolate(value,x,y):
    if value<x[0]:return y[0]+(value-x[0])*(y[1]-y[0])/(x[1]-x[0])
    if value>x[-1]:return y[-1]+(value-x[-1])*(y[-1]-y[-2])/(x[-1]-x[-2])
    return float(np.interp(value,x,y))


class SimulationMapping:
    """Continuous monotone angle mapping on either side of each calibrated zero."""
    def __init__(self, base, offsets, negative, positive, corrections=None):
        self.base=deepcopy(base)
        self.calibration=self.base.calibration
        self.middle=self.base.middle;self.trims=self.base.trims
        self.anchor=np.array([base.radians[n] for n in JOINTS])
        self.offsets=np.radians(offsets)
        self.negative=np.array(negative,dtype=float,copy=True);self.positive=np.array(positive,dtype=float,copy=True)
        self.radians=dict(zip(JOINTS,(self.anchor+self.offsets).tolist()))
        self.corrections=deepcopy(corrections or {})
        if not isinstance(self.corrections,dict) or set(self.corrections)-set(JOINTS):
            raise ValueError('시뮬 각도 보정표의 관절 이름을 확인하세요.')
        self.curves=None
        if self.corrections:
            self.curves=[]
            for i,name in enumerate(JOINTS):
                pairs=np.asarray(self.corrections.get(name,[[-180.,0.],[180.,0.]]),dtype=float)
                if (pairs.ndim!=2 or pairs.shape[1]!=2 or not 2<=len(pairs)<=128
                    or not np.isfinite(pairs).all() or np.any(np.diff(pairs[:,0])<=0)
                    or np.any(abs(pairs[:,0])>180) or np.any(abs(pairs[:,1])>20)):
                    raise ValueError(name+': 입력각 ±180°, 추가 보정 ±20°의 오름차순 보정점이 필요합니다.')
                x=np.unique(np.r_[-720.,pairs[:,0],0.,720.])
                y=offsets[i]+x*np.where(x<0,self.negative[i],self.positive[i])+np.interp(x,pairs[:,0],pairs[:,1])
                slope=np.diff(y)/np.diff(x)
                if np.any(slope<.1-1e-8) or np.any(slope>3.+1e-8):
                    raise ValueError(name+': 각도 변환은 단조 증가하고 기울기가 0.1~3 범위여야 합니다.')
                self.curves.append((x,y))
                self.radians[name]=float(self.anchor[i]+np.radians(_interpolate(0.,x,y)))

    def angles(self,ticks):
        return self.align_angles(self.base.angles(ticks))

    def align_angles(self,angles):
        """Convert original CAD joint angles without quantizing through motor ticks."""
        relative=np.asarray(angles)-self.anchor
        if self.curves is not None:
            return tuple(self.anchor+np.radians([_interpolate(v,*curve) for v,curve in zip(np.degrees(relative),self.curves)]))
        return tuple(self.anchor+self.offsets+relative*np.where(relative<0,self.negative,self.positive))

    def joint_angle(self,name,tick):
        i=JOINTS.index(name);relative=self.base.joint_angle(name,tick)-self.anchor[i]
        if self.curves is not None:return float(self.anchor[i]+np.radians(_interpolate(np.degrees(relative),*self.curves[i])))
        return float(self.anchor[i]+self.offsets[i]+relative*(self.negative[i] if relative<0 else self.positive[i]))

    def angle_limits(self,name):
        motor=self.calibration.motors[name]
        return sorted((self.joint_angle(name,motor.low),self.joint_angle(name,motor.high)))

    def unaligned_angles(self,angles):
        if self.curves is not None:
            relative=np.degrees(np.asarray(angles,dtype=float)-self.anchor)
            return self.anchor+np.radians([_interpolate(v,curve[1],curve[0]) for v,curve in zip(relative,self.curves)])
        relative=np.asarray(angles,dtype=float)-self.anchor-self.offsets
        return self.anchor+relative/np.where(relative<0,self.negative,self.positive)

    def ticks_from_angles(self,angles):
        return self.base.ticks_from_angles(self.unaligned_angles(angles))

    def tick_values(self,angles):
        if hasattr(self.base,'tick_values'):
            return self.base.tick_values(self.unaligned_angles(angles))
        mapping=self.calibration.angle_mapping;model=self.base
        return {n:(mapping.ticks(n,np.degrees(float(a)-model.radians[n])) if mapping else
                   model.middle[n]-model.trims[n]+(float(a)-model.radians[n])*4096/(2*np.pi))
                for n,a in zip(JOINTS,self.unaligned_angles(angles))}


CONFIG_DIR=ROOT/'assets/joint_alignment'
CONFIG_NAMES={'arm2':'joint_alignment.json','arm3':'arm3_joint_alignment.json'}


def reference_basis(reference):
    return {'ticks':reference.middle,'radians':reference.radians,'trim_ticks':reference.trims}


def load_alignment(profile,reference,config_dir=CONFIG_DIR):
    """Load the trial map only for the exact source encoder/model calibration."""
    robot=profile.get('robot_id','arm2')
    if robot not in CONFIG_NAMES:raise ValueError('지원하지 않는 로봇팔 보정입니다.')
    path=Path(config_dir)/CONFIG_NAMES[robot]
    try:raw=path.read_bytes();data=json.loads(raw)
    except (OSError,ValueError) as exc:raise ValueError('실행 보정 파일을 읽을 수 없습니다: '+path.name) from exc
    if not isinstance(data,dict):raise ValueError('실행 보정 파일 형식 오류')
    mapping=reference.calibration.angle_mapping
    if (data.get('schema')!=1 or data.get('robot_id')!=robot
        or data.get('joint_order')!=list(JOINTS)
        or data.get('calibration_sha256')!=reference.calibration.sha256
        or not mapping or data.get('angle_mapping_sha256')!=mapping.sha256
        or data.get('source_model_reference')!=reference_basis(reference)):
        raise ValueError('실행 보정의 원본 기준이 다릅니다. 팔·영점·3점 각도·모델 기준을 확인하세요.')
    if data.get('execution_trial_enabled') is not True:raise ValueError('실행 보정 시험이 활성화되지 않았습니다.')
    arrays=[np.asarray(data[k],float) for k in ('offset_deg','gains_negative','gains_positive')]
    if any(a.shape!=(6,) or not np.isfinite(a).all() for a in arrays):raise ValueError('실행 보정값 형식 오류')
    if np.any(abs(arrays[0])>15) or any(np.any((a<.7)|(a>1.5)) for a in arrays[1:]):
        raise ValueError('실행 보정값 범위 오류')
    aligned=SimulationMapping(reference,*arrays,data.get('correction_knots_deg'))
    aligned.alignment_sha256=hashlib.sha256(raw).hexdigest()
    return aligned


from .geometry import Kinematics


class ExecutionKinematics(Kinematics):
    """Offline trial replay only. Desktop and Pi execution use geometry.Kinematics."""
    def __init__(self,profile,reference,*,config_dir=CONFIG_DIR):
        self.base_reference=reference;self.alignment_error=None
        try:aligned=load_alignment(profile,reference,config_dir)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            aligned=reference;self.alignment_error=str(exc)
        super().__init__(aligned,tcp=profile.get('tcp'))

    def require_ready(self):
        if self.alignment_error:raise ValueError('실행 보정 계산 중단: '+self.alignment_error)

    def fk(self,ticks):
        self.require_ready()
        return super().fk(ticks)

    def taught_fk(self,ticks):
        """Keep the saved TCP anchor in the original measured-angle model.

        The execution mapping is used only to solve/evaluate motor commands;
        it must never redefine the jig-relative Cartesian teaching target.
        """
        self.require_ready()
        return self.fk_angles(self.base_reference.angles(ticks))
