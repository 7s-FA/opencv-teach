"""Seed a private demo workspace once; preserve saved episodes between runs."""
import json,os,shutil
from pathlib import Path

def default_data():
    base=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData/Local')) if os.name=='nt' else Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share'))
    return base/'SO101TeachDemo'/'v1'

def prepare_data(source,destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    seed=source/'examples/data'
    for name in ('calibration','episodes','jig_assets','inspection_reference'):
        if not (destination/name).exists():shutil.copytree(seed/name,destination/name)
    for name in ('profile.json','robot_profiles.json','preferences.json','jigs.json','model-adjustments.json','workcell-preview.json'):
        if not (destination/name).exists():shutil.copy2(seed/name,destination/name)
    prefixes=('/path/to/Final_Arm/SO101_teach','/home/fresh/Documents/Final_Arm/SO101_teach',str(source))
    def normalize(value,key=''):
        if isinstance(value,dict):return {k:normalize(v,k) for k,v in value.items()}
        if isinstance(value,list):return [normalize(v,key) for v in value]
        if key=='mode':return 'demo'
        if key=='device_host':return 'local'
        if key in ('token','password','identity_file'):return ''
        if key=='port' and isinstance(value,str):return 'DEMO'
        if isinstance(value,str):
            for prefix in prefixes:
                if value.startswith(prefix+'/data/'):return (destination/value[len(prefix)+6:]).as_posix()
                if value.startswith(prefix+'/'):return (source/value[len(prefix)+1:]).as_posix()
        return value
    for name in ('profile.json','robot_profiles.json','preferences.json','jigs.json'):
        file=destination/name;value=normalize(json.loads(file.read_text(encoding='utf-8')))
        if name=='preferences.json':value['device_host']='local'
        file.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return destination
