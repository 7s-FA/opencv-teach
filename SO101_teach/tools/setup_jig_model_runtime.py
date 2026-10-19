"""Prepare local/Pi image-only model providers; never access motors or camera."""
import argparse,json,shlex,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.domain import atomic_json
from so101_teach.pi_connection import load,validate
from so101_teach.remote_client import ssh_args

def main():
 p=argparse.ArgumentParser();p.add_argument('--python',type=Path,required=True);p.add_argument('--deploy-pi',action='store_true');args=p.parse_args()
 if not args.python.is_file():p.error('NCNN Python 환경이 없습니다.')
 config={'python':str(args.python),'app_dir':str(ROOT),'models':{s:str(ROOT/f'assets/models/yolo26{s}_mixed_fp16') for s in ('n','s')}}
 for path in config['models'].values():assert (Path(path)/'manifest.json').is_file()
 atomic_json(ROOT/'data/jig-model-runtime.json',config)
 if args.deploy_pi:
  connection=validate(load(ROOT/'data'),True);yolo=json.loads((ROOT/'data/yolo-runtime.json').read_text());remote_root=connection['app_dir']
  payload={'target':str(Path(remote_root)/'data/jig-model-runtime.json'),'config':{'python':yolo['pi_python'],'app_dir':yolo['pi_app_dir'],
    'models':{s:str(Path(yolo['pi_app_dir'])/f'assets/models/yolo26{s}_mixed_fp16') for s in ('n','s')}}}
  code="""import json,sys,os
from pathlib import Path
p=json.load(sys.stdin);c=p['config'];target=Path(p['target'])
assert Path(c['python']).is_file()
assert (Path(c['app_dir'])/'so101_teach/inference_protocol.py').is_file()
for path in c['models'].values():assert (Path(path)/'manifest.json').is_file()
raw=json.dumps(c,indent=2)+'\\n'
if target.exists() and target.read_text()!=raw:
 backup=target.with_suffix('.before-model-regions.json')
 if not backup.exists():backup.write_bytes(target.read_bytes())
temp=target.with_suffix('.tmp');temp.write_text(raw);os.replace(temp,target)
print('Pi image-only model configuration ready')
"""
  subprocess.run(ssh_args(connection)+['-l',connection['user'],connection['host'],'python3 -c '+shlex.quote(code)],input=json.dumps(payload),text=True,check=True,timeout=20)
 print('Local image-only model configuration ready')
if __name__=='__main__':main()
