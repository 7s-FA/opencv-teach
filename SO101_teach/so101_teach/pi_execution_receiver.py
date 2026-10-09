"""Run a GUI snapshot through the Pi CLI while retaining the PC's device leases.

This script is sent over authenticated SSH. Its stdin contains one snapshot then
control messages; losing stdin cancels the job. It never acquires a new arm lease
or sends heartbeats: the normal PC UI watchdog remains authoritative.
"""
import json
import math
import signal
from pathlib import Path
import sys
import tempfile
import threading


def validate_snapshot(cli, request):
    from so101_teach.domain import EpisodeStore,load_profile
    from so101_teach.remote_config import materialize
    from so101_teach.episode_inspection import criteria_signature
    if cli.INSPECTION_PROTOCOL != request.get('inspection_protocol'):
        raise ValueError('PC와 Pi의 에피소드 검사 실행 버전이 다릅니다.')
    if criteria_signature() != request.get('inspection_criteria_sha256'):
        raise ValueError('PC와 Pi의 안착 검사 기준이 다릅니다.')
    arm=request.get('arm');episode=request['episode'];bundle=request['bundle']
    if arm not in ('arm2','arm3') or episode.get('robot_id')!=arm or bundle['profile'].get('robot_id')!=arm:
        raise ValueError('실행 스냅샷의 로봇팔이 일치하지 않습니다.')
    if set(request['links'])!={'arm2','arm3'}:raise ValueError('두 팔의 연결이 필요합니다.')
    for link in request['links'].values():
        if type(link.get('port')) is not int or not 1<=link['port']<=65535 or not all(isinstance(link.get(k),str) and link[k] for k in ('token','lease','instance')):
            raise ValueError('Pi 제어 연결 정보가 올바르지 않습니다.')
    speed=request['speed'];timeout=request['timeout_seconds']
    if type(speed) not in (float,int) or not math.isfinite(speed) or not 1<=speed<=2000:
        raise ValueError('실행 속도가 올바르지 않습니다.')
    if type(timeout) not in (float,int) or not math.isfinite(timeout) or not 1<=timeout<=86400:
        raise ValueError('실행 제한시간이 올바르지 않습니다.')
    if type(request.get('apply_jig')) is not bool:raise ValueError('지그 보정 실행 방식 누락')
    data=cli.ROOT/('data' if arm=='arm2' else 'data/arm3-runtime')
    _,current_cal,_=load_profile(data)
    # Materialize only in a temporary directory during validation/check-only.
    with tempfile.TemporaryDirectory() as folder:
        materialize(folder,bundle)
        profile,cal,_=load_profile(Path(folder))
        if cal.sha256!=current_cal.sha256:raise ValueError('PC와 Pi의 모터 영점이 다릅니다.')
        EpisodeStore(Path(folder)/'episodes',cal,arm).validate(episode)
    if not episode['steps']:raise ValueError('실행할 스텝이 없습니다.')
    return data,episode,bundle,float(speed),float(timeout)


def install_adapters(cli,request,job,stream):
    """Adapt the existing CLI at its I/O boundaries, keeping its execution intact."""
    base_link=cli.Link;base_control=cli.Control;base_status=cli.Status
    cancelled=threading.Event();command_lock=threading.RLock()
    class BorrowedLink(base_link):
        def open(self):
            info=request['links'][self.arm]
            self.base='http://127.0.0.1:'+str(info['port']);self.token=info['token'];self.lease=info['lease']
            state=self.post('/state',{'lease':self.lease,'alive':False})
            if state['instance']!=info['instance'] or state['detached']:
                raise RuntimeError('앱의 Pi 제어 연결이 변경되었습니다.')
            self.rpc('claim_episode')
            follower=self.state()
            if not (cli.settled(follower) or cli.torque_off(follower)):
                raise RuntimeError('두 팔이 최신 정지 상태여야 에피소드를 실행할 수 있습니다.')
            # require_lease is also checked by every subsequent motion RPC.
        def state(self):
            # Unlike /state, this read passes require_lease on the Pi. A lost
            # UI/lease must stop the runner before it can command the linear stage.
            return super().rpc('execution_state')
        def rpc(self,method,args=None):
            if cancelled.is_set() and method not in ('camera_stop','detect_freeze') and not (method=='command' and (args or {}).get('action') in ('hold','release')):
                raise cli.ShutdownRequested('GUI_CANCELLED')
            if method=='command' and (args or {}).get('action') not in ('hold','release'):
                with command_lock:
                    if cancelled.is_set():raise cli.ShutdownRequested('GUI_CANCELLED')
                    return super().rpc(method,args)
            if method=='plan' and not request['apply_jig']:
                return {'plan':[{'ticks':step['ticks'],'corrected':False} for step in args['steps']]}
            return super().rpc(method,args)
        def close(self):
            # The GUI still owns and observes the controller after job completion.
            self.stop.set()
    class PipeControl(base_control):
        def __init__(self):
            super().__init__()
            signal.signal(signal.SIGHUP,lambda *_:self.cancel_execution())
            threading.Thread(target=self.read_controls,daemon=True).start()
        def read_controls(self):
            try:
                for line in stream:
                    command=line.strip()
                    if command=='cancel':self.cancel_execution()
                    elif command in ('estop','restart','reset'):
                        try:self.command(command)
                        except Exception as exc:print('CONTROL_REJECTED:'+str(exc),flush=True)
            finally:self.cancel_execution()
        def cancel_execution(self):
            cancelled.set();self.shutdown.set()
            with command_lock,self.lock:
                if self.emergency:self.emergency()
    class GuiStatus(base_status):
        def __init__(self,arm,command,ros,**kwargs):
            super().__init__(arm,request['episode'].get('product_type') or 'GUI',ros,**kwargs)
    cli.Link=BorrowedLink;cli.Control=PipeControl;cli.Status=GuiStatus
    cli.load_job=lambda arm,command:job


def main():
    root=Path(sys.argv[1]).expanduser().resolve()
    sys.path[:0]=[str(root),str(root/'integration')]
    import episode_cli as cli
    request=json.loads(sys.stdin.readline(32*1024*1024))
    job=validate_snapshot(cli,request)
    if '--check' in sys.argv[2:]:
        print('CHECK_OK GUI snapshot · '+request['arm']+' · '+str(len(job[1]['steps']))+' steps',flush=True)
        return 0
    install_adapters(cli,request,job,sys.stdin)
    # Only the selection of arm is encoded here. load_job returns the actual GUI
    # snapshot, never an A/B recipe lookup or an implicit overwrite of Pi files.
    command=('build_' if request['arm']=='arm2' else 'load_')+('b' if request['episode'].get('product_type')=='B' else 'a')
    return cli.main([command])


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        message='GUI_REJECTED:'+str(exc)
        if '--check' not in sys.argv[2:]:
            try:
                from action_protocol import record_notice
                record_notice(Path(sys.argv[1]).expanduser(),'workcell',message)
            except Exception:pass
        print(message,flush=True);raise SystemExit(1)
