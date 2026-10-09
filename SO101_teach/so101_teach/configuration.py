"""Project settings, asset provenance, and model-first TCP. No motor access."""
from copy import deepcopy
from pathlib import Path
import hashlib,uuid,xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from .domain import ROOT,JOINTS,Calibration,atomic_json,read_json

TCP_PRESETS={'고정 집게 끝단':'tip','고정 집게 중앙':'center'}

def tcp_label(tcp):
    if tcp.get('mode')=='manual':return '직접 설정'
    point=tcp.get('model_point','tip')
    return next(label for label,key in TCP_PRESETS.items() if key==point)

def model_tcp(model=ROOT/'assets/so101',*,point='tip'):
    if point not in TCP_PRESETS.values():raise ValueError('알 수 없는 모델 TCP 기준입니다.')
    model=Path(model);urdf=model/'so101_modified.urdf';tree=ET.parse(urdf).getroot()
    v=tree.find("link[@name='gripper_link']/visual[@name='xlerobot_fixed_finger']");j=tree.find("joint[@name='gripper_frame_joint']")
    def matrix(o):
        T=np.eye(4);T[:3,3]=np.fromstring(o.get('xyz','0 0 0'),sep=' ')*1000;T[:3,:3]=Rotation.from_euler('xyz',np.fromstring(o.get('rpy','0 0 0'),sep=' ')).as_matrix();return T
    mesh=v.find('geometry/mesh');path=model/mesh.get('filename');raw=path.read_bytes();count=int.from_bytes(raw[80:84],'little')
    if len(raw)!=84+50*count:raise ValueError('모델 STL 형식을 확인하세요.')
    data=np.frombuffer(raw[84:],dtype=np.dtype([('n','<f4',3),('v','<f4',(3,3)),('a','<u2')]))['v'].astype(float)
    scale=np.fromstring(mesh.get('scale','1 1 1'),sep=' ')*1000;T=np.linalg.inv(matrix(j.find('origin')))@matrix(v.find('origin'))
    points=(data*scale)@T[:3,:3].T+T[:3,3];verts=np.unique(points.reshape(-1,3),axis=0);tip=verts[verts[:,2]>=verts[:,2].max()-.01]
    if len(tip)!=2:raise ValueError('고정 집게 끝단 형상을 확인하세요.')
    return {'mode':'model','model_point':point,'xyz_mm':tip.mean(0).tolist() if point=='tip' else [0.,0.,0.],'rpy_deg':[0.,0.,0.], 'frame':'gripper_frame_link',
            'source_sha256':{str(p.relative_to(model)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (urdf,path)},'verified':False}

def tcp_matrix(tcp):
    xyz=np.asarray(tcp['xyz_mm'],float);rpy=np.asarray(tcp['rpy_deg'],float)
    if xyz.shape!=(3,) or rpy.shape!=(3,) or not np.isfinite([xyz,rpy]).all() or abs(xyz).max()>500 or abs(rpy).max()>360:raise ValueError('TCP 위치·각도 범위를 확인하세요.')
    T=np.eye(4);T[:3,3]=xyz;T[:3,:3]=Rotation.from_euler('xyz',rpy,degrees=True).as_matrix();return T

class JigCatalog:
    def __init__(self,data_dir,profile=None,*,persist_migration=False):
        self.root=Path(data_dir);self.path=self.root/'jigs.json'
        self.floor_profile=deepcopy(profile if profile is not None else read_json(self.root/'profile.json') if (self.root/'profile.json').exists() else {})
        self.items=read_json(self.path) if self.path.exists() else {'pallet':{'id':'pallet','name':'팔레트 70×70','stl':str(ROOT/'assets/jigs/pallet.stl'),'unit':'mm','shape':'ring','method':'edges','roi':None,'support_height_mm':0.}}
        from .height_reference import normalize_jig_height
        old_items=deepcopy(self.items)
        self.items={key:normalize_jig_height(item,self.floor_profile) for key,item in self.items.items()}
        for item in self.items.values():
            item.pop('yaw_deg',None);item.pop('heading_set',None)
            if item.get('method')=='white':item['method']='combined'
        if persist_migration and self.path.exists() and self.items!=old_items:atomic_json(self.path,self.items)
        self.revision=0;self.cache={}
    def save(self,item):
        from .vision import stl_profile
        d=deepcopy(item);name=d.get('name','').strip()
        if not name or len(name)>80:raise ValueError('지그 이름은 1~80자입니다.')
        if d.get('unit') not in ('mm','cm','m') or d.get('shape') not in ('rectangle','ring') or d.get('method') not in ('edges','combined','grid'):raise ValueError('지그 형상·단위·검출 방법을 선택하세요.')
        if d['method']=='grid' and (d['shape']!='rectangle' or not d.get('stl')):raise ValueError('외곽+교차점은 내부 격자가 있는 직사각형 STL이 필요합니다.')
        roi=d.get('roi')
        from .roi_geometry import roi_vertices
        roi_vertices(roi)
        d.pop('yaw_deg',None);d.pop('heading_set',None)
        from .height_reference import normalize_jig_height
        d=normalize_jig_height(d,self.floor_profile)
        if not d.get('stl'):
            size=[float(v) for v in d.get('manual_size_mm',[70,70])];height=float(d.get('manual_rim_mm',20))
            if len(size)!=2 or not np.isfinite(size).all() or min(size)<=0 or max(size)>2000 or not np.isfinite(height) or not 0<height<=500:raise ValueError('외곽 크기·테두리 높이를 확인하세요.')
            d.update(id=d.get('id') or uuid.uuid4().hex,name=name,stl=None,unit='mm',manual_size_mm=size,manual_rim_mm=height)
            atomic_json(self.path,{**self.items,d['id']:d});self.items[d['id']]=d;self.revision+=1;return d
        source=Path(d['stl']);raw=source.read_bytes();sha=hashlib.sha256(raw).hexdigest();dest=self.root/'jig_assets'/(sha+'.stl')
        # Validate before any catalog mutation.
        info=stl_profile(source,unit=d['unit'])
        if d['method']=='grid':
            from .grid_model import grid_layout
            if grid_layout({**info,'shape':d['shape']}) is None:raise ValueError('외곽+교차점은 큰 홈 또는 고정 지그 6개가 3×2로 배열된 STL을 지원합니다.')
        dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists():
            count=int.from_bytes(raw[80:84],'little') if len(raw)>=84 else 0
            if not count or len(raw)!=84+count*50:
                import re
                vertices=np.asarray(re.findall(r'\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)',raw.decode('ascii')),dtype=np.float32).reshape(-1,3,3)
                records=np.zeros(len(vertices),dtype=np.dtype([('n','<f4',3),('v','<f4',(3,3)),('a','<u2')]));records['v']=vertices
                raw=b'SO101 registered STL'.ljust(80,b'\0')+len(vertices).to_bytes(4,'little')+records.tobytes()
            dest.write_bytes(raw)
        d.update(id=d.get('id') or uuid.uuid4().hex,name=name,stl=str(dest),mesh=info)
        atomic_json(self.path,{**self.items,d['id']:d});self.items[d['id']]=d;self.revision+=1;return d
    def mesh(self,key):
        from .vision import stl_profile
        d=self.items[key]
        if not d.get('stl'):
            import json
            size=d['manual_size_mm'];h=d['manual_rim_mm'];sha=hashlib.sha256(json.dumps([size,h,d['shape']]).encode()).hexdigest()
            return {'sha256':sha,'size_mm':[*size,h],'rim_z_mm':h,'low_mm':[-size[0]/2,-size[1]/2,0],'holes':[],'shape':d['shape'],'method':d['method'],'unit':'mm','stl_path':None}
        stamp=(str(d['stl']),d['unit'],d['shape'],d['method'],Path(d['stl']).stat().st_mtime_ns)
        if stamp not in self.cache:
            m=stl_profile(d['stl'],unit=d['unit']);m.update(shape=d['shape'],method=d['method']);self.cache[stamp]=m
        return deepcopy(self.cache[stamp])
    def mesh_for_reference(self,key,reference):
        """Render saved teaching with its own STL, never a newer asymmetric mesh."""
        current=self.mesh(key);sha=reference.get('stl_sha256')
        if sha==current['sha256']:
            if current.get('assembly',{}).get('orientation_hole') and reference.get('symmetry_deg')!=360:return None
            return current
        if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):return None
        path=self.root/'jig_assets'/(sha+'.stl')
        if not path.is_file():return None
        stamp=('reference',sha,path.stat().st_mtime_ns)
        if stamp not in self.cache:
            from .vision import stl_profile
            value=stl_profile(path,unit='mm')
            self.cache[stamp]=value if value['sha256']==sha else None
        return deepcopy(self.cache[stamp])
    def duplicate(self,key):
        d=deepcopy(self.items[key]);d['id']=uuid.uuid4().hex;d['name']+=' 복사';return self.save(d)
    def remove(self,key,used=()):
        if key in used:raise ValueError('저장된 스텝에서 사용하는 지그는 삭제할 수 없습니다.')
        if len(self.items)==1:raise ValueError('지그는 하나 이상 남겨 주세요.')
        items={k:v for k,v in self.items.items() if k!=key};atomic_json(self.path,items);self.items=items;self.revision+=1

class ProfileLibrary:
    def __init__(self,data_dir):self.root=Path(data_dir);self.path=self.root/'robot_profiles.json';self.items=read_json(self.path) if self.path.exists() else {}
    def save(self,name,profile,ident=None):
        if not name.strip():raise ValueError('로봇 이름을 입력하세요.')
        p=deepcopy(profile);p['name']=name.strip();key=ident or uuid.uuid4().hex
        atomic_json(self.path,{**self.items,key:p});self.items[key]=p;return key
    def copy_calibration(self,path,role='follower'):
        from .angle_mapping import angle_path
        cal=Calibration(path);suffix=''
        if cal.angle_mapping:suffix='-a'+hashlib.sha256(angle_path(path).read_bytes()).hexdigest()[:12]
        target=self.root/'calibration'/f'{role}-{cal.sha256[:16]}{suffix}.json'
        # Preserve the exact calibration bytes: its SHA also identifies episodes and angle data.
        target.parent.mkdir(parents=True,exist_ok=True)
        if cal.angle_mapping:atomic_json(angle_path(target),cal.angle_mapping.document)
        elif angle_path(target).exists():raise ValueError('기존 3점 보정 파일과 충돌합니다.')
        if target.resolve()!=Path(path).resolve():
            import os,tempfile
            fd,tmp=tempfile.mkstemp(dir=target.parent)
            try:
                with os.fdopen(fd,'wb') as f:f.write(Path(path).read_bytes());f.flush();os.fsync(f.fileno())
                os.replace(tmp,target)
            finally:
                if Path(tmp).exists():Path(tmp).unlink()
        return str(target.relative_to(self.root)),Calibration(target)
    def reference_for(self,profile,cal):
        p=deepcopy(profile);p['model_reference']=deepcopy(p['model_reference']);r=p['model_reference']
        if cal.angle_mapping or r['calibration_sha256']!=cal.sha256:r.pop('trim_ticks',None)
        if cal.angle_mapping:r['angle_mapping_sha256']=cal.angle_mapping.sha256
        else:r.pop('angle_mapping_sha256',None)
        if r['calibration_sha256']==cal.sha256:return p
        r['calibration_sha256']=cal.sha256;r['ticks']=cal.ticks(dict.fromkeys(JOINTS,2047));return p
