"""SSH entry point for a supervised two-arm park and power-down."""
import fcntl
import json
from pathlib import Path
import sys
import time
import os
import threading


def park(cli, request, stream, emit,*,check=False,autonomous=False,lock_fd=None,on_accept=None):
    from so101_teach.pi_execution_receiver import install_adapters, validate_snapshot
    from episode_recovery import reset_workcell
    from linear_client import LinearClient
    records={}
    # Validate both snapshots before changing either controller.
    for arm in ('arm2','arm3'):
        part={**request,**request['arms'][arm],'arm':arm}
        _,episode,bundle,_,_=validate_snapshot(cli,part)
        first,last=episode['steps'][0],episode['steps'][-1]
        if (first.get('safe_boundary')!='start' or last.get('safe_boundary')!='end'
                or first['ticks']!=last['ticks'] or last.get('jig_id')):
            raise ValueError(arm+': 시작·종료 안전 자세가 필요합니다.')
        records[arm]={'schema':1,'state':'ready','arm':arm,'safe_ticks':last['ticks'],'bundle':bundle,
                      'calibration_sha256':episode['calibration_sha256'],'product':episode.get('product_type','GUI'),
                      'episode_id':episode['id'],'run_id':'app-shutdown','failed_at':time.time(),'error':''}
    if check:emit('SHUTDOWN_CHECK_OK');return
    if not autonomous:install_adapters(cli,{**request,'episode':request['arms']['arm2']['episode']},None,stream)
    base=cli.Link
    class ShutdownLink(base):
        def open(self):
            info=request['links'][self.arm]
            self.base='http://127.0.0.1:'+str(info['port']);self.token=info['token'];self.lease=info['lease']
            state=self.post('/state',{'lease':self.lease,'alive':False})
            if state['instance']!=info['instance'] or state['detached']:
                raise RuntimeError('앱의 Pi 제어 연결이 변경되었습니다.')
            if autonomous:
                result=self.rpc('handoff_shutdown',{'job_id':request['shutdown_id']})
                self.lease=result['lease']
                self.thread=threading.Thread(target=self.heartbeat,daemon=True);self.thread.start()
            else:self.rpc('claim_episode')
        def state(self):
            return self.rpc('execution_state') if autonomous else super().state()
        def close(self):
            if autonomous and not hasattr(self,'thread'):return
            super().close()
        def rpc(self,method,args=None):
            # Keep ownership until OFF is confirmed; no normal idle handoff.
            if method=='schedule_idle_release':return {}
            return super().rpc(method,args)
    links={};linear=None;control=None;control_thread=None;done=threading.Event()
    path=cli.ROOT/'data/episode-cli.lock'
    if lock_fd is not None and Path('/proc/self/fd/'+str(lock_fd)).resolve()!=path.resolve():raise ValueError('종료 작업 잠금 인계 오류')
    with (os.fdopen(lock_fd,'a') if lock_fd is not None else path.open('a')) as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('다른 작업이 실행 중입니다. 종료 복귀를 다시 시도하세요.') from None
        try:
            control=cli.Control()
            if autonomous:
                import signal
                signal.signal(signal.SIGTERM,lambda *_:control.shutdown.set())
                signal.signal(signal.SIGINT,lambda *_:control.shutdown.set())
                control.info={'arm':request.get('first','arm2'),'run_id':request['shutdown_id']}
                control_thread=threading.Thread(target=cli.control_server,args=(control,done),daemon=True);control_thread.start()
            for arm in ('arm2','arm3'):
                link=ShutdownLink(arm);links[arm]=link;link.open()
            def hold_all():
                for link in links.values():
                    try:link.rpc('command',{'action':'hold'})
                    except Exception:pass
            control.emergency=hold_all
            hold_all()
            if control.shutdown.is_set():raise RuntimeError('종료 작업 인계가 취소되었습니다.')
            if on_accept:on_accept()
            deadline=time.monotonic()+15
            while True:
                if control.shutdown.is_set():raise RuntimeError('종료 복귀가 중단되었습니다.')
                if all(cli.settled(link.state()) or cli.torque_off(link.state()) for link in links.values()):break
                if time.monotonic()>deadline:raise RuntimeError('두 팔의 정지 확인 시간 초과')
                time.sleep(.05)
            linear=LinearClient();linear.open()
            reset_workcell(cli,records,links,linear,control,emit,first=request.get('first','arm2'))
            control.emergency=hold_all
            emit('SHUTDOWN_SETTLING')
            wait_stable(cli,links,control)
            with control.lock:
                if control.pause.is_set() or control.shutdown.is_set():raise RuntimeError('사용자가 종료 복귀를 중단했습니다.')
                for link in links.values():link.rpc('command',{'action':'release'})
            deadline=time.monotonic()+10
            while not all(cli.torque_off(link.state()) for link in links.values()):
                if control.shutdown.is_set():raise RuntimeError('토크 해제 확인이 중단되었습니다.')
                if time.monotonic()>deadline:raise RuntimeError('두 팔의 토크 OFF 확인 시간 초과')
                time.sleep(.05)
            emit('SHUTDOWN_DONE')
        except BaseException:
            if control:
                for link in links.values():
                    try:link.rpc('command',{'action':'hold'})
                    except Exception:pass
            raise
        finally:
            done.set()
            if control:
                with control.lock:control.emergency=None
            if control_thread:control_thread.join(3)
            if linear:linear.close()
            for link in links.values():link.close()


def wait_stable(cli,links,control):
    deadline=time.monotonic()+30;baseline=None;since=None
    while True:
        if control.shutdown.is_set():raise RuntimeError('종료 복귀가 중단되었습니다.')
        if getattr(control,'pause',None) and control.pause.is_set():raise RuntimeError('사용자가 종료 복귀를 중단했습니다.')
        states={arm:link.state() for arm,link in links.items()}
        if not all(cli.settled(s) and not s.get('error') and len(s['latest']['ticks'])==6 for s in states.values()):
            raise RuntimeError('안전 자세 유지 확인 실패')
        ticks={arm:s['latest']['ticks'] for arm,s in states.items()}
        at=min(s['latest']['monotonic'] for s in states.values())
        if baseline is None or any(abs(ticks[a][n]-baseline[a][n])>2 for a in ticks for n in ticks[a]):
            baseline=ticks;since=max(at,time.monotonic())
        elif at-since>=3:return
        if time.monotonic()>deadline:raise RuntimeError('안전 자세 3초 정지 확인 시간 초과')
        time.sleep(.05)


if __name__=='__main__':
    root=Path(sys.argv[1]).expanduser().resolve();sys.path[:0]=[str(root),str(root/'integration')]
    import episode_cli as cli
    try:
        if '--dispatch' in sys.argv[2:]:
            from so101_teach.shutdown_worker import dispatch
            raw=b''
            while b'\n' not in raw:
                part=os.read(sys.stdin.fileno(),65536)
                if not part or len(raw)+len(part)>32*1024*1024:raise ValueError('종료 요청 수신 실패')
                raw+=part
            line,controls=raw.split(b'\n',1)
            dispatch(cli,json.loads(line),control_fd=sys.stdin.fileno(),controls=controls)
        else:
            request=json.loads(sys.stdin.readline(32*1024*1024))
            park(cli,request,sys.stdin,lambda event:print(event,flush=True),check='--check' in sys.argv[2:])
    except BaseException as exc:
        print('SHUTDOWN_FAILED:'+str(exc),flush=True);sys.exit(1)
