"""Portable configuration bundles; no arbitrary remote paths or code upload."""
from copy import deepcopy
from pathlib import Path,PurePosixPath
import base64,hashlib,json
from .domain import ROOT,read_json,atomic_json
PROTOCOL=1

def code_version():
    h=hashlib.sha256()
    for p in sorted((ROOT/'so101_teach').rglob('*.py')):
        h.update(str(p.relative_to(ROOT)).encode());h.update(p.read_bytes())
    for p in sorted((ROOT/'assets/so101').glob('*.urdf')):h.update(p.read_bytes())
    # Camera mount placement and CAD restore settings must match the Pi too.
    for name in ('assets/so101/preview_scene.xml','assets/so101/inspection_scene.xml','assets/reference/camera.json'):
        h.update(name.encode());h.update((ROOT/name).read_bytes())
    return h.hexdigest()

def configuration_bundle(app):
    profile=deepcopy(app.profile);files={}
    def calibration(name):
        p=app.data_dir/name;raw=p.read_bytes();side=p.with_suffix('.angles.json');extra=side.read_bytes() if side.exists() else b''
        key='calibration/'+hashlib.sha256(raw+extra).hexdigest()+'.json'
        files[key]=base64.b64encode(raw).decode()
        if extra:files[key.replace('.json','.angles.json')]=base64.b64encode(extra).decode()
        return key
    profile['calibration_file']=calibration(profile['calibration_file'])
    if profile.get('leader',{}).get('calibration_file'):profile['leader']['calibration_file']=calibration(profile['leader']['calibration_file'])
    jigs=deepcopy(app.catalog.items)
    for jig in jigs.values():
        if jig.get('stl'):
            raw=Path(jig['stl']).read_bytes();key='jig_assets/'+hashlib.sha256(raw).hexdigest()+'.stl'
            files[key]=base64.b64encode(raw).decode();jig['stl']=key
    return {'profile':profile,'jigs':jigs,'files':files,'trim_ticks':dict(app.reference.trims),'hold_seconds':app.pose_latch.seconds,
            'acquisition_seconds':app.pose_latch.stability.seconds,'acquisition_attempts':app.pose_latch.stability.attempts_limit,'active_jig':app.active_jig}

def bundle_digest(bundle):return hashlib.sha256(json.dumps(bundle,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def checked_files(bundle):
    out={};total=0
    for name,value in bundle.get('files',{}).items():
        p=PurePosixPath(name)
        if p.is_absolute() or len(p.parts)!=2 or p.parts[0] not in ('calibration','jig_assets') or p.parts[1] in ('.','..') or '\\' in name:
            raise ValueError('허용되지 않은 원격 파일 경로')
        if p.suffix not in ('.json','.stl'):raise ValueError('허용되지 않은 원격 파일 형식')
        raw=base64.b64decode(value,validate=True);total+=len(raw)
        if total>20*1024*1024:raise ValueError('설정 묶음이 너무 큽니다.')
        out[name]=raw
    return out

def materialize(data_dir,bundle):
    data_dir=Path(data_dir);files=checked_files(bundle);profile=deepcopy(bundle['profile']);jigs=deepcopy(bundle['jigs'])
    names=[profile['calibration_file']]
    if profile.get('leader',{}).get('calibration_file'):names.append(profile['leader']['calibration_file'])
    if any(n not in files or not n.startswith('calibration/') for n in names):raise ValueError('보정 파일 누락')
    for jig in jigs.values():
        if jig.get('stl'):
            if jig['stl'] not in files or not jig['stl'].startswith('jig_assets/'):raise ValueError('지그 STL 누락')
            jig['stl']=str(data_dir/jig['stl'])
    for name,raw in files.items():
        p=data_dir/name;p.parent.mkdir(parents=True,exist_ok=True)
        if p.exists() and p.read_bytes()!=raw:raise ValueError('기존 원격 설정 파일과 내용이 다릅니다.')
        if not p.exists():p.write_bytes(raw)
    atomic_json(data_dir/'profile.json',profile);atomic_json(data_dir/'jigs.json',jigs)
