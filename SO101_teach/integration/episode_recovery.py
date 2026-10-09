"""Explicit return to a recorded safe pose; never resume assembly automatically."""
from copy import deepcopy
from pathlib import Path
import fcntl
import signal
import threading
import time
from so101_teach.domain import atomic_json,read_json,load_profile


def path(root,arm):return Path(root)/'data'/f'episode-recovery-{arm}.json'


def invalidate(root,arm):
    file=path(root,arm)
    if file.exists():
        value=read_json(file);value['state']='superseded';atomic_json(file,value)


def save_failure(root,arm,product,episode,bundle,run_id,safe_ticks,error,*,failed_at=None,state="pending"):
    if safe_ticks is None:return
    atomic_json(path(root,arm),{'schema':1,'state':state,'arm':arm,'product':product,
        'episode_id':episode['id'],'run_id':run_id,'failed_at':time.time() if failed_at is None else failed_at,'error':str(error),
        'calibration_sha256':episode['calibration_sha256'],'safe_ticks':deepcopy(safe_ticks),'bundle':deepcopy(bundle)})


def save_ready(root,arm,product,episode,bundle,run_id,safe_ticks,*,recorded_at=None):
    save_failure(root,arm,product,episode,bundle,run_id,safe_ticks,'',state='ready',failed_at=recorded_at)


def reset_context(root,arm=None):
    choices=[]
    for name in (arm,) if arm else ('arm2','arm3'):
        file=path(root,name)
        if not file.exists():continue
        value=read_json(file)
        if value.get('state') in ('pending','ready','completed') and value.get('arm')==name:choices.append(value)
    if not choices:raise ValueError('저장된 안전 자세 복귀 정보가 없습니다.')
    return max(choices,key=lambda value:value.get('completed_at',value['failed_at']))


def pending(root,arm=None):
    choices=[]
    for name in (arm,) if arm else ('arm2','arm3'):
        file=path(root,name)
        if file.exists():
            value=read_json(file)
            if value.get('state')=='pending' and value.get('arm')==name:choices.append(value)
    if not choices:raise ValueError('복귀할 실패 작업 기록이 없습니다.')
    return max(choices,key=lambda value:value['failed_at'])


def workcell_records(cli):
    """Validate both saved poses before enabling either arm."""
    records={}
    for arm in ('arm2','arm3'):
        try:record=reset_context(cli.ROOT,arm)
        except ValueError:
            restore_legacy_failure(cli,arm);record=reset_context(cli.ROOT,arm)
        data=cli.ROOT/('data' if arm=='arm2' else 'data/arm3-runtime')
        profile,cal,_=load_profile(data)
        if profile.get('robot_id')!=arm or cal.sha256!=record['calibration_sha256']:
            raise ValueError(arm+': 저장된 로봇·영점과 현재 설정이 다릅니다.')
        record=deepcopy(record);record['safe_ticks']=cal.ticks(record['safe_ticks'])
        records[arm]=record
    return records


def reset_workcell(cli,records,links,linear,control,emit,*,active=None,first='arm2'):
    """Keep the workcell lease while returning each arm, then confirm once."""
    from so101_teach.motion import SPEED_PRESETS
    for arm in (first,'arm3' if first=='arm2' else 'arm2'):
        record=records[arm];link=links[arm];peer=links['arm3' if arm=='arm2' else 'arm2']
        report=lambda event,arm=arm:emit(event if event in ('PAUSED','RESUMED') else 'ARM_RESET:'+arm+':'+event)
        runner=active if active is not None and active.link is link else cli.Runner(link,report,control,120,peer=peer,linear=linear)
        old_emit=runner.emit;runner.emit=report;runner.safe_ticks=record['safe_ticks']
        control.emergency=runner.emergency
        try:
            state=peer.state()
            if state and state.get('running') and not (cli.settled(state) or cli.torque_off(state)):
                raise ValueError('다른 팔의 정지 확인이 필요합니다: '+arm)
            stage=linear.call('status')
            if stage.get('phase') not in ('TIMED_COMPLETE','MOVING'):
                raise ValueError('안전 복귀 전 리니어 정지 상태를 확인하세요.')
            if stage['phase']=='MOVING':
                runner.linear_active=True;runner.linear_target=stage['target_mm'];runner.linear_until=time.monotonic()+15
                runner.wait_linear()
            if runner is not active:
                runner.prepare_follower(record['bundle'],min(SPEED_PRESETS.values()))
            emit('ARM_RESET_STARTED:'+arm)
            while True:
                try:runner.reset_to_safe();break
                except cli.ResetRequested:continue
            record['state']='completed';record['completed_at']=time.time();atomic_json(path(cli.ROOT,arm),record)
            emit('ARM_RESET_DONE:'+arm)
        except BaseException:
            runner.emergency();raise
        finally:runner.emit=old_emit
    for link in links.values():link.rpc('schedule_idle_release')
    emit('RESET_DONE')


def run(cli,arm=None,*,ros=False):
    """Reuse the established safe-return motion after validating a saved failure."""
    from linear_client import LinearClient
    lock=link=peer=linear=status=runner=thread=None
    done=threading.Event();control=cli.Control()
    try:
        lock=(cli.ROOT/'data/episode-cli.lock').open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('BUSY: 다른 작업이 실행 중입니다.') from None
        if cli.control_path().exists():raise ValueError('실행 중인 작업의 reset을 사용하세요.')
        records=workcell_records(cli)
        # The request endpoint identifies the reporter, never limits reset scope.
        arm=arm or 'arm2';record=records[arm]
        status=cli.Status(arm,record['product'],ros);control.info={'arm':arm,'run_id':status.run_id}
        thread=threading.Thread(target=cli.control_server,args=(control,done),daemon=True);thread.start()
        signal.signal(signal.SIGINT,lambda *_:control.command('estop'))
        signal.signal(signal.SIGTERM,lambda *_:control.shutdown.set())
        link,peer=cli.Link('arm2'),cli.Link('arm3');link.open();peer.open()
        linear=LinearClient();linear.open()
        reset_workcell(cli,records,{'arm2':link,'arm3':peer},linear,control,status.emit)
        return 0
    except BaseException as exc:
        if runner:runner.emergency()
        if status:status.emit('FAILED:RESET:'+str(exc))
        else:print('REJECTED:RESET:'+str(exc),flush=True)
        return 1
    finally:
        done.set()
        with control.lock:control.emergency=None
        if thread:thread.join(3)
        if linear:
            try:linear.close()
            except Exception:pass
        for connection in (link,peer):
            if connection and connection.lease:
                try:connection.close()
                except Exception:pass
        control.emergency=None
        if lock:lock.close()
        if status:
            try:status.flush_final()
            finally:status.close()


def restore_legacy_failure(cli,arm=None):
    """Recover a legacy job only with matching failure or unchanged completed recipe."""
    for name in (arm,) if arm else ('arm2','arm3'):
        latest=cli.ROOT/'data'/f'episode-cli-{name}.json'
        if not latest.exists():continue
        status=read_json(latest);text=status.get('status','')
        if text in ('A_DONE','B_DONE'):
            data,episode,bundle,_,_=cli.load_job(name,text[0])
            sources=[cli.ROOT/'integration/recipes.json',data/'episodes'/(episode['id']+'.json')]
            if any(not source.exists() or source.stat().st_mtime>status['at'] for source in sources):continue
            end=episode['steps'][-1]
            if end.get('safe_boundary')=='end' and not end.get('jig_id'):
                save_ready(cli.ROOT,name,text[0],episode,bundle,status['run_id'],end['ticks'],recorded_at=status['at'])
            continue
        if not (text.startswith('A_FAILED:') or text.startswith('B_FAILED:')):continue
        _,episode,bundle,_,_=cli.load_job(name,text[0])
        logs=sorted((cli.ROOT/'data/diagnostics').glob('episode-inspection-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for file in logs[:30]:
            diagnostic=read_json(file);checks=diagnostic.get('checks',[])
            if diagnostic.get('episode_id')!=episode['id'] or not checks:continue
            last=checks[-1]
            if last.get('result')!='FAIL' or abs(last.get('at',0)-status.get('at',0))>5 or not last.get('error') or last['error'] not in text:continue
            end=episode['steps'][-1]
            if end.get('safe_boundary')=='end' and not end.get('jig_id'):
                save_failure(cli.ROOT,name,text[0],episode,bundle,status['run_id'],end['ticks'],last['error'],failed_at=status['at'])
            break
