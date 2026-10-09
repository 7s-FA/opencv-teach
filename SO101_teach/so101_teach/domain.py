"""Validated raw-tick data, immutable calibration, and atomic episode storage."""
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import math
import os
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
JOINTS=('shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper')
LABELS=('베이스 회전','어깨 들기','팔꿈치','손목 굽힘','손목 회전','집게 벌림')

def read_json(path):
    def reject(x):raise ValueError('유효하지 않은 JSON 숫자: '+x)
    return json.loads(Path(path).read_text(),parse_constant=reject)

def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    fd,tmp=tempfile.mkstemp(prefix='.writing-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

@dataclass(frozen=True)
class Motor:
    id:int
    homing:int
    low:int
    high:int

class Calibration:
    def __init__(self,path):
        path=Path(path);self.sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        data=read_json(path)
        if set(data)!=set(JOINTS):raise ValueError('SO-101 서보 6개의 보정값이 필요합니다.')
        self.motors={}
        for i,name in enumerate(JOINTS,1):
            row=data[name]
            for key in ('id','drive_mode','homing_offset','range_min','range_max'):
                if type(row.get(key)) is not int:raise ValueError(name+': 정수 보정값이 필요합니다.')
            if row['id']!=i or row['drive_mode']!=0 or not -2047<=row['homing_offset']<=2047:
                raise ValueError(name+': ID·회전 방향·영점 범위를 확인하세요.')
            if not 0<=row['range_min']<row['range_max']<=4095:raise ValueError(name+': 잘못된 틱 범위')
            self.motors[name]=Motor(i,row['homing_offset'],row['range_min'],row['range_max'])
        from .angle_mapping import AngleMapping,angle_path
        self.angle_mapping=AngleMapping(read_json(angle_path(path)),self.sha256,self.motors) if angle_path(path).exists() else None
    def ticks(self,values,within_limits=True):
        if not isinstance(values,dict) or set(values)!=set(JOINTS):raise ValueError('6개 서보 틱이 모두 필요합니다.')
        result={}
        for n in JOINTS:
            v=values[n];m=self.motors[n]
            if type(v) is not int or not 0<=v<=4095:raise ValueError(n+': 0~4095의 정수 틱을 입력하세요.')
            if within_limits and not m.low<=v<=m.high:raise ValueError(f'{n}: {v}틱은 측정 범위 {m.low}~{m.high} 밖입니다.')
            result[n]=v
        return result

class ModelReference:
    def __init__(self,calibration,data):
        self.calibration=calibration
        if data.get('calibration_sha256')!=calibration.sha256:raise ValueError('모델 기준과 보정 JSON이 다릅니다.')
        if data.get('encoder_counts_per_turn')!=4096:raise ValueError('SO-101은 1회전 4096틱 기준입니다.')
        expected_angles=data.get('angle_mapping_sha256')
        if expected_angles and (not calibration.angle_mapping or calibration.angle_mapping.sha256!=expected_angles):raise ValueError('프로필에 연결된 3점 각도 파일이 없거나 변경되었습니다. 보정 파일을 다시 선택하세요.')
        self.middle=calibration.ticks(data['ticks']);self.trims=dict.fromkeys(JOINTS,0)
        self.radians=data['radians']
        if set(self.radians)!=set(JOINTS) or any(type(v) not in (int,float) or not math.isfinite(v) for v in self.radians.values()):
            raise ValueError('모델 기준각이 올바르지 않습니다.')
        self.set_trims(data.get('trim_ticks',dict.fromkeys(JOINTS,0)))
    def set_trims(self,values):
        if set(values)!=set(JOINTS) or any(type(v) is not int or abs(v)>512 for v in values.values()):raise ValueError('시뮬 보정은 서보별 ±512틱의 정수입니다.')
        if self.calibration.angle_mapping and any(values.values()):raise ValueError('3점 각도 보정이 적용되어 Δ틱 보정을 중복 적용하지 않습니다.')
        self.trims=dict(values)
    def angles(self,ticks):
        ticks=self.calibration.ticks(ticks,within_limits=False)
        return tuple(self.joint_angle(n,ticks[n]) for n in JOINTS)
    def joint_angle(self,name,tick):
        mapping=self.calibration.angle_mapping
        return self.radians[name]+(math.radians(mapping.degrees(name,tick)) if mapping else (tick-self.middle[name]+self.trims[name])*2*math.pi/4096)
    def angle_limits(self,name):
        m=self.calibration.motors[name]
        return sorted((self.joint_angle(name,m.low),self.joint_angle(name,m.high)))
    def ticks_from_angles(self,angles):
        if len(angles)!=6 or not all(math.isfinite(a) for a in angles):raise ValueError('6개 관절각이 필요합니다.')
        mapping=self.calibration.angle_mapping
        return self.calibration.ticks({n:round(mapping.ticks(n,math.degrees(float(a)-self.radians[n])) if mapping else self.middle[n]-self.trims[n]+(float(a)-self.radians[n])*4096/(2*math.pi)) for n,a in zip(JOINTS,angles)})

@dataclass(frozen=True)
class Snapshot:
    role:str
    ticks:dict
    telemetry:dict
    monotonic:float
    wall_time:float
    calibration_sha256:str
    calibration_matches:bool
    port:str
    def fresh(self,now=None):return 0<=(time.monotonic() if now is None else now)-self.monotonic<=.5

class EpisodeStore:
    def __init__(self,directory,calibration,robot_id=None):self.directory=Path(directory);self.calibration=calibration;self.robot_id=robot_id
    def new(self,name='새 에피소드'):
        return {**({'robot_id':self.robot_id} if self.robot_id else {}),'schema':1,'id':uuid.uuid4().hex,'name':name,'unit':'motor_ticks','calibration_sha256':self.calibration.sha256,
                'steps':[],'jig_references':{},'created_at':time.time(),'inspection_timing':'step_complete'}
    def validate(self,doc):
        if doc.get('schema')!=1 or doc.get('unit')!='motor_ticks':raise ValueError('새 앱의 모터 틱 형식 에피소드가 아닙니다.')
        if self.robot_id and doc.get('robot_id','arm2')!=self.robot_id:raise ValueError('다른 로봇팔의 에피소드입니다. 선택한 팔을 확인하세요.')
        if doc.get('calibration_sha256')!=self.calibration.sha256:raise ValueError('다른 영점으로 저장된 에피소드입니다. 자동 변환하지 않습니다.')
        if not isinstance(doc.get('name'),str) or not doc['name'].strip() or len(doc['name'])>120:raise ValueError('에피소드 이름은 1~120자입니다.')
        if not isinstance(doc.get('id'),str) or len(doc['id'])!=32 or any(c not in '0123456789abcdef' for c in doc['id']):raise ValueError('에피소드 ID 오류')
        if not isinstance(doc.get('steps'),list) or len(doc['steps'])>500:raise ValueError('스텝 목록 오류')
        ids=set()
        for s in doc['steps']:
            if not isinstance(s.get('id'),str) or s['id'] in ids:raise ValueError('스텝 ID 중복 또는 누락')
            ids.add(s['id'])
            if not isinstance(s.get('name'),str) or not s['name'].strip():raise ValueError('스텝 이름을 입력하세요.')
            self.calibration.ticks(s['ticks'])
            if s.get('jig_id') is not None:
                if not isinstance(s['jig_id'],str) or not s['jig_id']:raise ValueError('지그 ID가 없습니다.')
                r=s.get('jig_reference',{});pose=r.get('pose')
                if not isinstance(pose,list) or len(pose)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) for v in pose):raise ValueError('스텝의 티칭 지그 기준이 없습니다.')
                if r.get('symmetry_deg') not in (90,180,360) or not isinstance(r.get('stl_sha256'),str) or len(r['stl_sha256'])!=64:raise ValueError('지그 STL·각도 기준 오류')
                from .jig_heading import mesh_yaw_offset
                mesh_yaw_offset(r)
        from .episode_events import validate_events
        validate_events(doc)
        from .episode_inspection import validate_inspection
        validate_inspection(doc)
        marked=[(i,s.get('safe_boundary')) for i,s in enumerate(doc['steps']) if s.get('safe_boundary')]
        if marked:
            if marked!=[(0,'start'),(len(doc['steps'])-1,'end')] or len(doc['steps'])<2:raise ValueError('안전 자세는 처음과 끝에 배치하세요.')
            if doc['steps'][0]['ticks']!=doc['steps'][-1]['ticks'] or any(s.get('jig_id') for s in (doc['steps'][0],doc['steps'][-1])):raise ValueError('시작·종료 안전 자세는 같은 고정 관절값이어야 합니다.')
        return doc
    def save(self,doc):
        self.validate(doc);path=self.directory/(doc['id']+'.json');atomic_json(path,doc);return path
    def load(self,path):
        from .episode_inspection import migrate_inspection_timing
        return self.validate(migrate_inspection_timing(read_json(path)))
    def delete(self,episode_id):
        if not isinstance(episode_id,str) or len(episode_id)!=32 or any(c not in '0123456789abcdef' for c in episode_id):raise ValueError('에피소드 ID 오류')
        path=self.directory/(episode_id+'.json');doc=self.load(path)
        if doc['id']!=episode_id:raise ValueError('선택한 에피소드와 저장 파일이 다릅니다.')
        path.unlink()
    def entries(self):
        self.directory.mkdir(parents=True,exist_ok=True);items=[]
        for p in sorted(self.directory.glob('*.json')):
            try:items.append((p,self.load(p)))
            except (OSError,ValueError,KeyError,TypeError):continue
        return items
    def capture(self,snapshot,name):
        if not snapshot.fresh():raise ValueError('최신 실물 위치가 없습니다. 연결 상태를 확인하세요.')
        if snapshot.role!='follower' or not snapshot.calibration_matches or snapshot.calibration_sha256!=self.calibration.sha256:
            raise ValueError('팔로워 영점 확인이 필요합니다.')
        return self.step(snapshot.ticks,name,{'kind':'measured','port':snapshot.port,'wall_time':snapshot.wall_time})
    def step(self,ticks,name,source=None):
        if not name.strip():raise ValueError('스텝 이름을 입력하세요.')
        return {'id':uuid.uuid4().hex,'name':name.strip(),'ticks':self.calibration.ticks(ticks),'jig_id':None,
                'source':source or {'kind':'edited'},'saved_at':time.time()}

def load_profile(data_dir=None,*,profile=None):
    root=Path(data_dir or ROOT/'data');p=deepcopy(profile) if profile is not None else read_json(root/'profile.json')
    p.setdefault('mode','leader')
    cal=Calibration(root/p['calibration_file']);ref=ModelReference(cal,p['model_reference'])
    return p,cal,ref
