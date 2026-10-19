"""Configure the PC engine and deploy image-only Pi files into a separate folder.

Prepare the Pi venv with requirements-yolo.txt before using --deploy-pi.
The robot server, serial devices, and its environment are never opened here.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from so101_teach.domain import atomic_json
from so101_teach.pi_connection import load,validate
from so101_teach.remote_client import ssh_args


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir',type=Path,default=ROOT/'data')
    p.add_argument('--pc-python',type=Path,required=True)
    p.add_argument('--sam3-python',type=Path)
    p.add_argument('--n-benchmark',type=Path,help='Completed n/s FP16 comparison directory')
    p.add_argument('--pi-app-dir',default='/home/ubuntu/so101-yolo')
    p.add_argument('--deploy-pi',action='store_true')
    args=p.parse_args()
    if not args.pc_python.is_file():p.error('PC Python 실행 파일이 없습니다.')
    remote_path=Path(args.pi_app_dir)
    if not remote_path.is_absolute() or remote_path.name!='so101-yolo':
        p.error('독립 배포 폴더는 절대 경로이며 이름이 so101-yolo여야 합니다.')
    config_path=args.data_dir/'yolo-runtime.json'
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    config.update(pc_python=str(args.pc_python.absolute()),pi_python=str(remote_path/'.venv/bin/python'),
                  pi_app_dir=str(remote_path),pi_model='assets/models/yolo26s_mixed_fp16',
                  pi_n_model='assets/models/yolo26n_mixed_fp16')
    if args.sam3_python:
        if not args.sam3_python.is_file():p.error('SAM3 Python 실행 파일이 없습니다.')
        config['sam3_python']=str(args.sam3_python.absolute())
    if args.n_benchmark:
        result=json.loads((args.n_benchmark/'comparison.json').read_text())
        if result['status']!='complete':p.error('n/s FP16 비교가 아직 완료되지 않았습니다.')
        source=args.n_benchmark/'n_ncnn_model'
        files=result['weights']['n']['fp16_files']
        if set(files)!={'model.ncnn.bin','model.ncnn.param','metadata.yaml'}:
            p.error('n 모델 파일 목록이 올바르지 않습니다.')
        for name,digest in files.items():
            if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:
                p.error('n 모델 검증 해시가 다릅니다: '+name)
        destination=ROOT/config['pi_n_model'];destination.mkdir(exist_ok=True)
        for name in files:shutil.copy2(source/name,destination/name)
        atomic_json(destination/'manifest.json',{'model':'YOLO26n-seg','training':'real+synthetic',
                    'format':'NCNN FP16','imgsz':960,'files':files,'comparison':str(args.n_benchmark)})
    if args.deploy_pi:
        connection=validate(load(args.data_dir),True)
        files=[Path('so101_teach')/name for name in ('__init__.py','yolo_worker.py','yolo_inference.py','inference_protocol.py')]
        files += [Path('assets/fonts')/name for name in ('NanumGothic.ttf','NanumGothic-LICENSE.txt')]
        files += [path.relative_to(ROOT) for path in (ROOT/config['pi_model']).iterdir() if path.is_file()]
        if (ROOT/config['pi_n_model']/'manifest.json').exists():
            files += [path.relative_to(ROOT) for path in (ROOT/config['pi_n_model']).iterdir() if path.is_file()]
        files += [Path('requirements-yolo.txt')]
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w:gz') as bundle:
            for path in files:bundle.add(ROOT/path,arcname=str(path),recursive=False)
        command='test -x '+shlex.quote(config['pi_python'])+' && tar -xzf - -C '+shlex.quote(str(remote_path))
        subprocess.run(ssh_args(connection)+['-l',connection['user'],connection['host'],command],input=archive.getvalue(),check=True,timeout=90)
    atomic_json(args.data_dir/'yolo-runtime.json',config)
    print(json.dumps(config,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
