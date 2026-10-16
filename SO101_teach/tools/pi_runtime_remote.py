"""Internal SSH worker for pi_runtime.py; stdout contains no control credentials."""
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,urlopen


def run(request):
    root=Path(request['root']).expanduser().resolve()
    namespace={};exec(request['manifest_source'],namespace)
    manifest=namespace['manifest'];difference=namespace['difference']
    def peers():
        result={};private={}
        for arm,folder in [('arm2',root/'data'),('arm3',root/'data/arm3-runtime')]:
            meta=folder/'remote-server.json'
            if not meta.exists():result[arm]={'running':False};continue
            info=json.loads(meta.read_text());token=(folder/'remote-token').read_text().strip()
            def call(endpoint,body=None,info=info,token=token):
                req=Request('http://127.0.0.1:'+str(info['port'])+endpoint,data=json.dumps(body or {}).encode(),
                    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urlopen(req,timeout=3) as response:value=json.load(response)
                if not value.get('ok'):raise RuntimeError(value.get('error','Pi response failed'))
                return value['value']
            try:
                health=call('/health');state=call('/state');camera=call('/camera')
            except OSError:
                # A live but unresponsive PID must not be treated as an idle server.
                if Path('/proc/'+str(info['pid'])).exists():raise RuntimeError(arm+': 실행부 응답 없음; 갱신하지 않았습니다.')
                result[arm]={'running':False};continue
            if health['instance']!=info['instance']:raise RuntimeError(arm+': 실행부 식별 정보 변경')
            result[arm]={'running':True,'runtime':health.get('runtime'),'protocol':health['protocol'],
                'version':health['version'],'detached':state['detached'],'camera_running':camera.get('running',False),
                'devices_running':{k:bool((state.get(k) or {}).get('running')) for k in ('follower','leader','calibration')}}
            private[arm]=(info,call)
        return result,private
    def inspect():
        servers,_=peers();disk=manifest(root,allow_missing=True)
        return {'disk':disk,'servers':servers,'changed_files':difference(request['expected'],disk),
            'restart_required':[a for a,s in servers.items() if not s['running'] or difference(disk,s.get('runtime'))]}
    if request['operation']=='check':return inspect()
    (root/'data').mkdir(exist_ok=True)
    with (root/'data/runtime-update.lock').open('a') as update,(root/'data/episode-cli.lock').open('a') as work:
        fcntl.flock(update,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB)
        servers,private=peers()
        for arm,state in servers.items():
            if state['running'] and (not state['detached'] or state['camera_running'] or any(state['devices_running'].values())):
                raise RuntimeError(arm+': 앱·팔로워·카메라·보정 연결을 정상 종료한 뒤 다시 동기화하세요. 장치를 강제로 끊지 않았습니다.')
        motion=Path.home()/'.local/state/arm3-linear-controller/motion.json'
        if motion.exists() and json.loads(motion.read_text()).get('phase') in ('MOVING','PAUSED','STARTING'):
            raise RuntimeError('리니어 작업이 진행 중이므로 갱신하지 않았습니다.')
        disk=manifest(root,allow_missing=True)
        if disk['sha256']!=request['observed_sha256']:raise RuntimeError('Pi 파일이 점검 이후 변경되었습니다. check부터 다시 실행하세요.')
        expected=request['expected'];changed=difference(expected,disk)
        extras=set(disk['files'])-set(expected['files'])
        if extras:raise RuntimeError('Pi에만 있는 실행 파일은 자동 삭제하지 않습니다: '+', '.join(sorted(extras)))
        files=request['files']
        if set(files)!={p for p in changed if p in expected['files']}:raise RuntimeError('동기화 파일 목록 불일치')
        for name,encoded in files.items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or str(path) not in expected['files']:raise RuntimeError('허용되지 않은 파일 경로')
            raw=base64.b64decode(encoded,validate=True)
            if hashlib.sha256(raw).hexdigest()!=expected['files'][name]:raise RuntimeError('전송 파일 해시 불일치: '+name)
            if name.endswith('.py'):compile(raw,name,'exec')
        stale=any(not s['running'] or difference(expected,s.get('runtime')) for s in servers.values())
        if not changed and not stale:return {**inspect(),'applied':False,'reason':'이미 동기화되어 있습니다.'}
        service='workcell-actions.service'
        service_active=subprocess.run(['systemctl','is-active','--quiet',service]).returncode==0
        # ActionServer only keeps these two project modules loaded. Episode and
        # device code is launched in fresh child processes for each command.
        restart_service=service_active and bool(set(changed)&{'integration/action_server.py','integration/action_protocol.py'})
        if restart_service:
            allowed=subprocess.run(['sudo','-n','-l','/usr/bin/systemctl','restart',service],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if allowed.returncode:raise RuntimeError('기존 액션 서비스 재시작 권한이 없어 변경 전에 중단했습니다.')
        backup=root/'verification'/('runtime-sync-'+time.strftime('%Y%m%d-%H%M%S')+'-'+str(os.getpid()))
        backup.mkdir(parents=True)
        absent=[];stopped=[];reserved={}
        def stop_peer(arm,info):
            proc=Path('/proc/'+str(info['pid']))
            if not proc.exists():return
            if (proc/'cwd').resolve()!=root or b'so101_teach.remote_server' not in (proc/'cmdline').read_bytes().split(b'\0'):
                raise RuntimeError('종료 대상 프로세스 식별 실패: '+arm)
            os.kill(info['pid'],signal.SIGTERM)
            deadline=time.monotonic()+5
            while proc.exists():
                if time.monotonic()>deadline:raise RuntimeError('유휴 실행부 종료 시간 초과: '+arm)
                time.sleep(.05)
        def start_peers():
            for arm in ('arm2','arm3'):
                result=subprocess.run([sys.executable,'-B','-m','so101_teach.remote_server','--ensure','--arm',arm],cwd=root,
                    text=True,capture_output=True,timeout=20,env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'})
                if result.returncode:raise RuntimeError(arm+' 실행부 시작 실패: '+result.stderr[-500:])
                # ensure's stdout contains a token: never return or log it.
                health=json.loads(result.stdout)
                if difference(manifest(root),health.get('runtime')):raise RuntimeError(arm+' 실행부 버전 적용 실패')
        try:
            for arm,(info,call) in private.items():
                # Old runtimes do not yet honor runtime-update.lock. A temporary
                # lease blocks competing clients while stopping an idle old peer.
                # New runtimes honor the exclusive update lock, so no lease is needed.
                if not servers[arm].get('runtime'):
                    reserved[arm]=call('/acquire')['lease']
                stop_peer(arm,info);stopped.append(arm)
            for name,encoded in files.items():
                target=root/name;old=backup/name;old.parent.mkdir(parents=True,exist_ok=True)
                if target.exists():shutil.copy2(target,old)
                else:absent.append(name)
                target.parent.mkdir(parents=True,exist_ok=True)
                stage=target.with_name(target.name+'.runtime-sync');stage.write_bytes(base64.b64decode(encoded));os.replace(stage,target)
                if target.suffix=='.py':
                    for cache in (target.parent/'__pycache__').glob(target.stem+'.*.pyc'):cache.unlink()
            namespace['validate_boundaries'](root)
            if difference(expected,manifest(root)):raise RuntimeError('적용 후 실행부 해시 불일치')
            start_peers()
            if restart_service:subprocess.run(['sudo','-n','systemctl','restart',service],check=True,timeout=20)
            result=inspect()
            if result['changed_files'] or result['restart_required']:raise RuntimeError('실행 중인 버전 확인 실패')
            result.update(applied=True,backup=str(backup),updated_files=sorted(files),action_service_restarted=restart_service)
            (backup/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            return result
        except BaseException:
            # Prevent another app from acquiring half-updated controllers; restore
            # original files before releasing either deployment lock.
            live,details=peers()
            for arm,(info,_) in details.items():
                if not any(live[arm]['devices_running'].values()):stop_peer(arm,info)
            for name in files:
                target=root/name;old=backup/name
                if old.exists():shutil.copy2(old,target)
                elif name in absent:target.unlink(missing_ok=True)
                for cache in (target.parent/'__pycache__').glob(target.stem+'.*.pyc'):cache.unlink()
            (backup/'rollback.json').write_text(json.dumps({'restored':True,'files':list(files)}))
            # Leave restored peers stopped; reconnect/ensure starts the restored
            # version. Never claim an unsuccessful deployment as completed.
            raise


if __name__=='__main__':
    try:
        print(json.dumps({'ok':True,'result':run(json.load(sys.stdin))},ensure_ascii=False),flush=True)
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False),flush=True)
        sys.exit(1)
