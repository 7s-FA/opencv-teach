"""Measured servo angles, separate from firmware zero and mechanical stops."""
import math,json,hashlib
from copy import deepcopy
from .domain import JOINTS


def angle_path(path):
    from pathlib import Path
    return Path(path).with_suffix('.angles.json')

def points_ready(name,points):
    if name=='gripper' and points.get('kind')=='gripper_range':return all(k in points for k in ('zero','closed','open'))
    return all(k in points for k in ('zero','negative','positive'))


class AngleMapping:
    def __init__(self,document,calibration_sha,motors):
        if document.get('schema')!=1 or document.get('calibration_sha256')!=calibration_sha:
            raise ValueError('3점 각도 보정과 모터 영점 JSON이 다릅니다.')
        joints=document.get('joints',{})
        if set(joints)!=set(JOINTS):raise ValueError('몸체 5개 관절의 3점과 집게 보정 기록이 필요합니다.')
        self.document=deepcopy(document);self.sha256=hashlib.sha256(json.dumps(document,sort_keys=True,allow_nan=False).encode()).hexdigest();self.points={};self.gripper_anchor=None
        for name in JOINTS:
            points=joints[name];values=[]
            if name=='gripper' and isinstance(points,dict) and points.get('kind')=='gripper_range':
                anchor=points.get('zero',{});tick=anchor.get('ticks');lo=points.get('closed',{}).get('ticks');hi=points.get('open',{}).get('ticks')
                if any(type(v) is not int for v in (tick,lo,hi)) or anchor.get('degrees')!=-90 or abs(tick-2047)>5:raise ValueError('집게의 −90° 기준 틱을 확인하세요.')
                if not motors[name].low==lo<=tick<hi==motors[name].high:raise ValueError('집게는 닫힘 최소 틱 ≤ 기준 틱 < 열림 최대 틱이어야 합니다.')
                self.gripper_anchor=tick;self.points[name]=[(lo,-90+(lo-tick)*360/4096),(tick,-90.),(hi,-90+(hi-tick)*360/4096)];continue
            for key in ('negative','zero','positive'):
                p=points.get(key,{}) if isinstance(points,dict) else {};tick=p.get('ticks');deg=p.get('degrees')
                if type(tick) is not int or type(deg) not in (int,float) or not math.isfinite(deg):raise ValueError(name+': 각도·틱 기록 형식 오류')
                if not motors[name].low<=tick<=motors[name].high:raise ValueError(name+': 각도 기준점이 측정한 움직임 범위 밖입니다.')
                values.append((tick,float(deg)))
            (tn,an),(tz,az),(tp,ap)=values
            if not -180<=an<0==az<ap<=180 or abs(tz-2047)>5:raise ValueError(name+': 음수 각도·0°·양수 각도와 영점 2047을 확인하세요.')
            if not tn<tz<tp:raise ValueError(name+': 음수 틱 < 영점 틱 < 양수 틱 순서여야 합니다.')
            self.points[name]=values
    def degrees(self,name,tick):
        if name=='gripper' and self.gripper_anchor is not None:return -90+(tick-self.gripper_anchor)*360/4096
        (tn,an),(tz,_),(tp,ap)=self.points[name]
        t,a=(tn,an) if (tick-tz)*(tn-tz)>=0 else (tp,ap)
        return (tick-tz)*a/(t-tz)
    def ticks(self,name,degrees):
        if name=='gripper' and self.gripper_anchor is not None:return self.gripper_anchor+(degrees+90)*4096/360
        (tn,an),(tz,_),(tp,ap)=self.points[name]
        t,a=(tn,an) if degrees<0 else (tp,ap)
        return tz+degrees*(t-tz)/a
    def extrapolated(self,name,tick):
        values=[p[0] for p in self.points[name]]
        return not min(values)<=tick<=max(values)
