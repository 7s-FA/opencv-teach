"""Check runtime compatibility or explicitly synchronize an idle Pi; no motion."""
import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from so101_teach.runtime_version import manifest,validate_boundaries,difference
from so101_teach.pi_connection import load,validate
from so101_teach.remote_client import ssh_args


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('local','check','sync'))
    parser.add_argument('--apply',action='store_true',help='Apply only after verifying all Pi devices/clients are idle')
    parser.add_argument('--output',type=Path,help='Save a credential-free JSON report')
    args=parser.parse_args(argv)
    validate_boundaries(ROOT);local=manifest(ROOT)
    if args.operation=='local':
        print('Pi 실행 코드 경계·필수 자산 확인: '+str(len(local['files']))+'개 파일');return 0
    config=validate(load(ROOT/'data'),True)
    worker=Path(__file__).with_name('pi_runtime_remote.py').read_text()
    def remote(payload):
        request={'root':config['app_dir'],'manifest_source':(ROOT/'so101_teach/runtime_version.py').read_text(),'expected':local,**payload}
        command=shlex.quote(config['python'])+' -B -c '+shlex.quote(worker)
        result=subprocess.run([*ssh_args(config),'-l',config['user'],config['host'],command],input=json.dumps(request),
            capture_output=True,text=True,timeout=90)
        try:reply=json.loads(result.stdout)
        except ValueError:raise RuntimeError('Pi 점검 응답 실패: '+result.stderr[-500:]) from None
        if not reply.get('ok'):raise RuntimeError(reply.get('error','Pi 점검 실패'))
        return reply['result']
    report=remote({'operation':'check'})
    if args.operation=='sync' and args.apply:
        names=report['changed_files']
        files={name:base64.b64encode((ROOT/name).read_bytes()).decode() for name in names if name in local['files']}
        report=remote({'operation':'apply','observed_sha256':report['disk']['sha256'],'files':files})
    report['local']=local
    ready=not report['changed_files'] and not report['restart_required'] and all(s['running'] for s in report['servers'].values())
    report['compatible']=ready
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('PC–Pi 실행 버전: '+('일치' if ready else '갱신 필요'))
    for name in report['changed_files']:print('  변경: '+name)
    for arm in report['restart_required']:print('  실행부 시작·재시작 필요: '+arm)
    if report.get('applied'):print('백업: '+report['backup'])
    if args.operation=='sync' and not args.apply:print('변경하지 않았습니다. 적용하려면 sync --apply를 사용하세요.')
    return 0 if ready else 2


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:print(str(exc),file=sys.stderr);sys.exit(1)
