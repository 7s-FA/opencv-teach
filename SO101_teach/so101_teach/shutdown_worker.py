"""Detached Pi-owned shutdown. Durable progress contains no control credentials."""
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid


def dispatch(cli,request,*,control_fd=None,controls=b''):
    request={**request,'shutdown_id':uuid.uuid4().hex}
    folder=cli.ROOT/'data/shutdown-jobs'/request['shutdown_id'];folder.mkdir(parents=True,mode=0o700)
    file=folder/'request.json'
    with (cli.ROOT/'data/episode-cli.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('다른 작업의 종료를 기다린 뒤 다시 종료하세요.') from None
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(request,stream,ensure_ascii=False,allow_nan=False)
        read_fd,write_fd=os.pipe()
        try:
            with (folder/'worker.log').open('ab') as log:
                process=subprocess.Popen([sys.executable,'-B','-m','so101_teach.shutdown_worker',str(file),str(lock.fileno()),str(write_fd)],
                    cwd=cli.ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                    pass_fds=(lock.fileno(),write_fd),start_new_session=True)
            os.close(write_fd);write_fd=None
            deadline=time.monotonic()+25
            while True:
                if b'cancel' in controls.splitlines():
                    process.terminate()
                    raise RuntimeError('사용자가 Pi 종료 작업 인계를 취소했습니다.')
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    process.terminate()
                    raise RuntimeError('Pi 종료 작업 인계 응답 시간 초과 · 작업 '+request['shutdown_id'])
                ready=select.select([read_fd]+([control_fd] if control_fd is not None else []),[],[],remaining)[0]
                if control_fd in ready:
                    data=os.read(control_fd,1024)
                    if data:controls+=data;continue
                    control_fd=None
                if read_fd in ready:break
            reply=os.read(read_fd,4096).decode().strip()
            if reply!='ACCEPTED':
                status=folder/'status.json'
                detail=json.loads(status.read_text()).get('status','') if status.exists() else ''
                raise RuntimeError('Pi 종료 작업 인계 실패: '+detail)
            print('SHUTDOWN_ACCEPTED:'+request['shutdown_id'],flush=True)
        finally:
            os.close(read_fd)
            if write_fd is not None:os.close(write_fd)


def run(file,lock_fd,ack_fd):
    from .domain import ROOT,atomic_json
    from .shutdown_receiver import park
    sys.path.insert(0,str(ROOT/'integration'))
    import episode_cli as cli
    request=json.loads(file.read_text());file.unlink()
    key=request['shutdown_id'];events=[];folder=file.parent;accepted=False;completed=False
    def emit(event):
        nonlocal completed
        if event=='SHUTDOWN_DONE':completed=True;return
        row={'run_id':key,'arm':request.get('first','arm2'),'status':event if event.startswith('SHUTDOWN_') else 'SHUTDOWN_'+event,'at':time.time()}
        events.append(row)
        atomic_json(ROOT/'data/episode-cli-runs'/(key+'.json'),{'run_id':key,'events':events})
        atomic_json(folder/'status.json',{**row,'accepted':accepted,'pid':os.getpid()})
    def acknowledge():
        nonlocal accepted,ack_fd
        accepted=True;emit('ACCEPTED')
        os.write(ack_fd,b'ACCEPTED\n');os.close(ack_fd);ack_fd=None
    try:
        emit('STARTING')
        park(cli,request,None,emit,autonomous=True,lock_fd=lock_fd,on_accept=acknowledge)
    except BaseException as exc:
        emit('FAILED:'+str(exc));return 1
    finally:
        if ack_fd is not None:os.close(ack_fd)
    # The workcell lock and both leases are returned before announcing DONE.
    if completed:
        row={'run_id':key,'arm':request.get('first','arm2'),'status':'SHUTDOWN_DONE','at':time.time()}
        events.append(row)
        atomic_json(ROOT/'data/episode-cli-runs'/(key+'.json'),{'run_id':key,'events':events})
        atomic_json(folder/'status.json',{**row,'accepted':accepted,'pid':os.getpid()})
    return 0


if __name__=='__main__':sys.exit(run(Path(sys.argv[1]),int(sys.argv[2]),int(sys.argv[3])))
