"""Explicit return to a recorded safe pose; never resume assembly automatically."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import fcntl
from concurrent.futures import ThreadPoolExecutor
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
        # Recovery restores the saved motion basis, not an obsolete AI model
        # selection. configure() persists this bundle for subsequent jobs too.
        saved_profile=record['bundle'].setdefault('profile',{})
        for key in ('jig_detection','part_inspection'):
            if key in profile:saved_profile[key]=deepcopy(profile[key])
            else:saved_profile.pop(key,None)
        records[arm]=record
    return records


def reset_workcell(cli,records,links,linear,control,emit,*,active=None,first='arm2',arms=('arm2','arm3'),startup=False):
    """Keep the workcell lease and confirm only after every safe return."""
    from so101_teach.motion import SPEED_PRESETS
    if not arms or not set(arms)<={'arm2','arm3'} or set(links)!=set(arms) or set(records)!=set(arms) or first not in arms:
        raise ValueError('안전 복귀할 팔·연결·저장 자세가 일치하지 않습니다.')
    if len(arms)==2:
        return reset_pair(cli,records,links,linear,control,emit,active=active,first=first,startup=startup)
    for arm in (first,*(a for a in arms if a!=first)):
        record=records[arm];link=links[arm];peer=links.get('arm3' if arm=='arm2' else 'arm2')
        report=lambda event,arm=arm:emit(event if event in ('PAUSED','RESUMED') else 'ARM_RESET:'+arm+':'+event)
        runner=active if active is not None and active.link is link else cli.Runner(link,report,control,120,peer=peer,linear=linear)
        old_emit=runner.emit;runner.emit=report;runner.safe_ticks=record['safe_ticks']
        control.emergency=runner.emergency
        try:
            state=peer.state() if peer else None
            if state and state.get('running') and not (cli.settled(state) or cli.torque_off(state)):
                raise ValueError('다른 팔의 정지 확인이 필요합니다: '+arm)
            stage=linear.call('status')
            if startup and stage.get('phase')!='TIMED_COMPLETE':
                raise ValueError('시작 자세 준비 전 리니어 정지 확인 필요 · 리니어 이동 없음')
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
    if not startup:
        for link in links.values():link.rpc('schedule_idle_release')
    if not startup:
        control.complete('reset');emit('RESET_DONE')


def prepare_chain_start(cli,records,links,linear,control,emit,*,active,speed,on_started):
    """Prepare both start poses without recording RESET or releasing torque.

    Unlike recovery, an already confirmed start pose can be reused, and the
    validated linear movement may overlap the two safe-start movements.
    """
    return reset_pair(cli,records,links,linear,control,emit,active=active,first='arm2',
        startup=True,persist=False,prepare_active=True,active_speed=speed,
        on_started=on_started,skip_matching=True)


def validate_pair_start(cli,order,runners,links,linear,*,startup):
    # Validate both arms before preparing or enabling either one.
    for arm in order:
        if runners[arm].safe_ticks is None:raise RuntimeError(arm+': 저장된 안전 자세가 없습니다.')
        s=links[arm].state()
        if s and s.get('running') and not (cli.settled(s) or cli.torque_off(s) or cli.activation_paused(s)):
            raise ValueError('안전 복귀 전 두 팔의 정지 확인이 필요합니다: '+arm)
    stage=linear.call('status')
    if startup and stage.get('phase')!='TIMED_COMPLETE':raise ValueError('시작 자세 준비 전 리니어 정지 확인 필요 · 리니어 이동 없음')
    if stage.get('phase') not in ('TIMED_COMPLETE','MOVING'):raise ValueError('안전 복귀 전 리니어 정지 상태를 확인하세요.')
    return stage


def prepare_pair_followers(preparing,records,*,active,active_speed):
    from so101_teach.motion import SPEED_PRESETS
    try:
        for runner in preparing.values():runner.follower_phase='before_connect'
        for runner in preparing.values():runner.check();runner.emit('FOLLOWER_CONNECTING')
        with ThreadPoolExecutor(max_workers=2,thread_name_prefix='safe-connect') as pool:
            speeds={arm:active_speed if runner is active else min(SPEED_PRESETS.values()) for arm,runner in preparing.items()}
            futures={arm:pool.submit(runner.start_follower,records[arm]['bundle'],speeds[arm]) for arm,runner in preparing.items()}
            for arm,runner in preparing.items():runner.finish_follower(futures[arm].result(),speeds[arm])
    finally:
        for runner in preparing.values():runner.follower_phase='active'


class PairRecovery:
    """One coordinator owns paired recovery, pauses and deadlines.

    Stages return False only after a confirmed pause/resume requires a fresh
    attempt. Device/state failures propagate to HOLD; they are never retried.
    """
    def __init__(self,cli,records,links,linear,control,emit,*,active,first,startup,persist,prepare_active,active_speed,on_started,skip_matching):
        self.cli,self.records,self.links,self.linear=cli,records,links,linear
        self.control,self.emit,self.active=control,emit,active
        self.first,self.startup,self.persist=first,startup,persist
        self.prepare_active,self.active_speed=prepare_active,active_speed
        self.on_started,self.skip_matching=on_started,skip_matching
        self.order=(self.first,'arm3' if self.first=='arm2' else 'arm2');self.runners={};self.old_emit={}
        self.previous_emergency=self.control.emergency;self.deadline=time.monotonic()+120
        for arm in self.order:
            runner=self.active if self.active is not None and self.active.link is self.links[arm] else self.cli.Runner(
                self.links[arm],self.emit,self.control,120,peer=self.links[self.order[1] if arm==self.order[0] else self.order[0]],linear=self.linear)
            self.runners[arm]=runner;self.old_emit[arm]=runner.emit
            runner.emit=lambda event,arm=arm:self.emit(event if event in ('PAUSED','RESUMED') else 'ARM_RESET:'+arm+':'+event)
            runner.safe_ticks=self.records[arm]['safe_ticks']

    def hold_all(self):
        errors=[]
        for arm,runner in self.runners.items():
            try:errors.extend(arm+': '+e for e in runner.emergency())
            except Exception as exc:errors.append(arm+': '+str(exc))
        return errors

    def state(self,arm, *, preparing=False):
        if self.control.shutdown.is_set():raise self.cli.ShutdownRequested('PROCESS_SHUTDOWN')
        if not self.control.pause.is_set() and time.monotonic()>=self.deadline:raise RuntimeError('두 팔 안전 복귀 시간 초과')
        value=self.runners[arm].state();sample=(value or {}).get('latest') or {}
        # Each servo's guarded torque activation pauses aggregate samples.
        # Only the pre-motion readiness loop may wait through activation and
        # the first HOLD sample; state transitions precede aggregate telemetry.
        # settled() still requires a fresh HOLD sample before targets are sent.
        if (preparing and value and value.get('running') and not value.get('error')
                and value.get('state') in ('READ_ONLY', 'ACTIVATING', 'ACTIVATION_PAUSED', 'HOLD') and sample.get('calibration_matches')
                and len(sample.get('telemetry', {})) == 6):
            return value
        if (not value or not value.get('running') or value.get('error') or not sample.get('calibration_matches')
                or not 0<=time.monotonic()-sample.get('monotonic',0)<.5
                or len(sample.get('telemetry',{}))!=6):
            raise RuntimeError(arm+': 안전 복귀 중 최신 모터 상태를 확인할 수 없습니다.')
        return value

    def paused(self):
        if self.control.shutdown.is_set():raise self.cli.ShutdownRequested('PROCESS_SHUTDOWN')
        if not self.control.pause.is_set():return False
        began=time.monotonic();errors=self.hold_all()
        if errors:raise RuntimeError('두 팔 정지 요청 실패: '+'; '.join(errors))
        while True:
            observations=[self.state(arm) for arm in self.order]
            now=time.monotonic()
            if all(self.cli.stopped(s,now=now) for s in observations) and self.control.confirm_estop(observations):break
            if time.monotonic()-began>15:raise RuntimeError('두 팔 정지 확인 시간 초과')
            time.sleep(.05)
        self.emit('PAUSED')
        while True:
            observations=[self.state(arm) for arm in self.order]
            now=time.monotonic()
            for arm,s in zip(self.order,observations):
                if not self.cli.stopped(s,now=now):raise RuntimeError(arm+': 비상정지 중 자세 유지 상태 변경')
            with self.control.lock:
                self.control.consume_supervision_stop()
                stop_ready=self.control.confirm_estop(observations)
                if stop_ready and (self.control.resume.is_set() or self.control.reset.is_set()):
                    restarting=self.control.resume.is_set()
                    self.control.resume.clear();self.control.reset.clear();self.control.pause.clear();self.control.paused=False
                    if restarting:self.control.complete('restart')
                    break
            time.sleep(.05)
        elapsed=time.monotonic()-began;self.deadline+=elapsed
        for runner in self.runners.values():runner.deadline+=elapsed
        self.emit('RESUMED');return True

    def run(self):
        self.control.emergency=self.hold_all
        try:
            self.prepare()
            while True:
                self.paused()
                if not self.enable_torque():continue
                if not self.wait_for_holds():continue
                if not self.send_targets():continue
                self.recheck_skipped()
                if self.paused():continue
                # This callback belongs only to validated chain-start preparation.
                # Consume it once; a resumed attempt must not repeat the linear goal.
                if self.on_started is not None:
                    if self.on_started() is False:continue
                    self.on_started=None
                if self.wait_for_arrivals():break
            return self.finish()
        except BaseException:
            self.hold_all();raise
        finally:
            self.control.emergency=self.previous_emergency
            for arm,runner in self.runners.items():runner.emit=self.old_emit[arm]

    def prepare(self):
        stage=validate_pair_start(self.cli,self.order,self.runners,self.links,self.linear,startup=self.startup)
        preparing={arm:runner for arm,runner in self.runners.items() if runner is not self.active or self.prepare_active}
        prepare_pair_followers(preparing,self.records,active=self.active,active_speed=self.active_speed)
        for runner in self.runners.values():
            if runner.assembly_evidence:runner.assembly_evidence.clear_run()
            runner.emit('RESET_STARTED')
        if stage['phase']=='MOVING':
            runner=self.runners[self.first];runner.linear_active=True;runner.linear_target=stage['target_mm'];runner.linear_until=time.monotonic()+15
            runner.emit('RESET_WAIT_LINEAR_COMPLETE');runner.wait_linear()

    def enable_torque(self):
        self.torque_started={}
        # Enable both and confirm both are holding before either return move.
        for arm in self.order:
            if self.paused():return False
            s=self.state(arm)
            if self.cli.torque_off(s) or self.cli.activation_paused(s):
                self.torque_started[arm]=time.monotonic()
                self.runners[arm].emit('RESET_TORQUE_ON_STARTED');self.links[arm].rpc('command',{'action':'arm'})
            elif not self.cli.settled(s):raise RuntimeError(arm+': 안전 복귀 전 정지 자세 확인 실패')
        return True

    def wait_for_holds(self):
        ready_until=time.monotonic()+15
        while True:
            if self.paused():return False
            ready=[]
            for arm in self.order:
                holding=self.cli.settled(self.state(arm,preparing=True));ready.append(holding)
                if holding and arm in self.torque_started:
                    began=self.torque_started.pop(arm)
                    if getattr(self.control,'startup_timing',None):self.control.startup_timing.add(arm+'.torque.ready',time.monotonic()-began)
            if all(ready):break
            if time.monotonic()>=ready_until:raise RuntimeError('두 팔 토크 ON 확인 시간 초과')
            time.sleep(.05)
        return True

    def send_targets(self):
        self.requests={};self.skipped={}
        for arm in self.order:
            if self.paused():return False
            self.runners[arm].emit('RESET_TORQUE_ON_CONFIRMED')
            s=self.state(arm) if self.skip_matching else None
            if self.skip_matching and self.cli.at_safe_pose(s,self.runners[arm].safe_ticks):
                self.requests[arm]=s.get('request_id');self.skipped[arm]=s.get('completed_request_id')
                self.emit('ARM_SAFE_ALREADY:'+arm)
                continue
            self.emit('ARM_RESET_STARTED:'+arm)
            self.runners[arm].emit('SAFE_RETURN_STARTED')
            self.requests[arm]=self.links[arm].rpc('command',{'action':'move','targets':[self.runners[arm].safe_ticks]})['request_id']
        return True

    def still_at_start(self,arm,s,*,now=None):
        return (self.cli.at_safe_pose(s,self.runners[arm].safe_ticks,now=now)
                and s.get('request_id')==self.requests[arm] and s.get('completed_request_id')==self.skipped[arm])

    def recheck_skipped(self):
        # Re-read skipped arms after all dispatch I/O and before the carriage
        # starts. A changed pose/request must not reuse the earlier check.
        observations={arm:self.state(arm) for arm in self.skipped};now=time.monotonic()
        for arm,s in observations.items():
            if not self.still_at_start(arm,s,now=now):raise RuntimeError(arm+': 이동 생략 후 시작 안전자세가 변경되었습니다.')

    def wait_for_arrivals(self):
        self.done=set()
        while len(self.done)<len(self.order):
            if self.paused():return False
            if self.active is not None and self.active.linear_active:self.active.poll_linear()
            for arm in self.order:
                s=self.state(arm)
                if self.control.pause.is_set():break
                if arm in self.skipped:
                    if not self.still_at_start(arm,s):raise RuntimeError(arm+': 이동 생략 후 시작 안전자세가 변경되었습니다.')
                    self.done.add(arm);continue
                if s.get('state') not in ('HOLD','MOVING'):raise RuntimeError(arm+': 안전 복귀 중 자세 유지 상태 변경')
                if s.get('completed_request_id')==self.requests[arm] and self.cli.settled(s):
                    if arm not in self.done:self.runners[arm].emit('SAFE_RETURN_DONE');self.done.add(arm)
                elif arm in self.done or s.get('request_id')==self.requests[arm] and self.cli.settled(s):
                    raise RuntimeError(arm+': 안전 복귀 중 보호 정지 · 자동 재개하지 않습니다.')
            if len(self.done)<len(self.order):time.sleep(.05)
        return True

    def finish(self):
        for arm in self.order:
            if self.persist:
                record=self.records[arm];record['state']='completed';record['completed_at']=time.time();atomic_json(path(self.cli.ROOT,arm),record)
            self.runners[arm].emit('RESET_DONE');self.emit('ARM_RESET_DONE:'+arm)
        if not self.startup:
            for link in self.links.values():link.rpc('schedule_idle_release')
            self.control.complete('reset');self.emit('RESET_DONE')
        if self.skip_matching:
            return {arm:{'request_id':self.requests[arm],'ticks':deepcopy(self.runners[arm].safe_ticks),
                         **({'confirmed_without_move':True,'completed_request_id':self.skipped[arm]} if arm in self.skipped else {})}
                    for arm in self.order}
        return self.requests


def reset_pair(cli,records,links,linear,control,emit,*,active,first,startup,persist=True,prepare_active=False,active_speed=None,on_started=None,skip_matching=False):
    return PairRecovery(cli,records,links,linear,control,emit,active=active,first=first,
        startup=startup,persist=persist,prepare_active=prepare_active,active_speed=active_speed,
        on_started=on_started,skip_matching=skip_matching).run()


def run(cli,arm=None,*,ros=False,startup=False,execution_lock_fd=None,managed=False,supervision_job_id=None,stop_fd=None,status_fd=None,supervision_client_id=None):
    """Reuse the established safe-return motion after validating a saved failure."""
    from linear_client import LinearClient
    lock=link=peer=linear=status=runner=thread=preparation=None
    done=threading.Event();control=cli.Control()
    stop_thread=control.configure_supervision(supervision_job_id,stop_fd,done)
    try:
        if execution_lock_fd is not None:
            import os
            path=cli.ROOT/'data/episode-cli.lock'
            if Path(f'/proc/self/fd/{execution_lock_fd}').resolve()!=path.resolve():raise ValueError('실행 잠금 인계 오류')
            lock=os.fdopen(execution_lock_fd,'a')
        else:lock=(cli.ROOT/'data/episode-cli.lock').open('a')
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('BUSY: 다른 작업이 실행 중입니다.') from None
        if cli.control_path().exists():raise ValueError('실행 중인 작업의 reset을 사용하세요.')
        records=workcell_records(cli)
        if startup:
            from standby_preparation import WorkcellPreparation
            preparation_cli=cli.preparation_context()
            standby_records=WorkcellPreparation.records(SimpleNamespace(slide_only=True,control=control),preparation_cli)
        # The request endpoint identifies the reporter, never limits reset scope.
        arm=arm or 'arm2';record=records[arm]
        status=cli.Status(arm,'HOME' if startup else record['product'],ros,**({'status_fd':status_fd} if status_fd is not None else {}));status.managed=managed
        status.control=control;status.timing.job_id=control.job_id
        status.configure_analysis('prepare_home' if startup else 'reset',request_id=supervision_job_id)
        control.info={'arm':arm,'run_id':status.run_id}
        thread=threading.Thread(target=cli.control_server,args=(control,done),daemon=True);thread.start()
        signal.signal(signal.SIGINT,lambda *_:control.command('estop'))
        signal.signal(signal.SIGTERM,lambda *_:control.shutdown.set())
        link,peer=cli.Link('arm2'),cli.Link('arm3');cli.open_links(link,peer)
        linear=LinearClient();linear.open()
        if startup:
            runner=cli.Runner(link,status.emit,control,180,peer=peer,linear=linear)
            preparation=WorkcellPreparation(SimpleNamespace(link=link,peer=peer,control=control,
                args=SimpleNamespace(supervision_client_id=supervision_client_id),runner=runner,
                status=status,linear=linear,standby_records=standby_records,pc_command=False),preparation_cli)
            status.emit('WORKCELL_PREPARING');preparation.begin()
        reset_workcell(cli,records,{'arm2':link,'arm3':peer},linear,control,status.emit,startup=startup)
        if startup:
            # Both arms reached their saved safe poses before retracting the
            # carriage. Use the existing retracted preset, never a guessed 0mm.
            control.emergency=runner.emergency
            runner.prepare_linear(1.5)
            # The return used the saved motion basis. Readiness uses current
            # recipes/ROI/models; old recovery bundles must not replace them.
            for name,connection in (('arm2',link),('arm3',peer)):
                runner.check();connection.rpc('configure',{'bundle':standby_records[name]['bundle']})
            preparation.finish()
            status.emit('RESET_DONE')
        return 0
    except BaseException as exc:
        if preparation:preparation.committed=False
        if runner:runner.emergency()
        if status:
            status.analysis.error(exc,'recovery');status.emit('FAILED:RESET:'+str(exc))
        else:print('REJECTED:RESET:'+str(exc),flush=True)
        return 1
    finally:
        if preparation is not None and not preparation.committed:preparation.cancel()
        try:cli.finish_controls(control,(link,peer))
        except Exception as exc:print('제어 완료 기록 정리 실패: '+str(exc))
        done.set()
        if stop_thread:stop_thread.join(1)
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
