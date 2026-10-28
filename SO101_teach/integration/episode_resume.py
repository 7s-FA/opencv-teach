"""Operator-driven, read-only rechecks while the original job retains ownership."""
from copy import deepcopy
import math
import time
import uuid
from so101_teach.episode_faults import RetryableFault,InspectionRetry,ResumeBlocked,RecheckInterrupted


class PoseGuard:
    def __init__(self,state):
        self.state=deepcopy(state)
    def validate(self,current,stopped):
        from so101_teach.motion import BODY_ARRIVAL_TOLERANCE_TICKS
        baseline=self.state;old=(baseline or {}).get('latest') or {};sample=(current or {}).get('latest') or {}
        a=old.get('ticks') or {};b=sample.get('ticks') or {}
        if not stopped(current):raise RuntimeError('재개 전 최신 모터 정지 상태를 확인할 수 없습니다.')
        if (current.get('state')!=baseline.get('state') or current.get('request_id')!=baseline.get('request_id')
                or current.get('completed_request_id')!=baseline.get('completed_request_id')
                or sample.get('calibration_sha256')!=old.get('calibration_sha256')
                or len(a)!=6 or a.keys()!=b.keys()
                or any(type(a[k]) not in (int,float) or not math.isfinite(a[k]) or type(b[k]) not in (int,float) or not math.isfinite(b[k]) or abs(b[k]-a[k])>BODY_ARRIVAL_TOLERANCE_TICKS for k in a)):
            raise ResumeBlocked('정지한 체크포인트의 자세·실행 요청이 변경되었습니다. 원래 상태를 확인하거나 RESET 하세요.')
        torque=1 if current['state']=='HOLD' else 0
        if not sample.get('calibration_matches') or any(v.get('torque')!=torque for v in sample['telemetry'].values()):
            raise ResumeBlocked('재개 전 보정·토크 상태 확인이 필요합니다.')


def same_jigs(before,after):
    from so101_teach.jig_consensus import angle_delta
    if before.keys()!=after.keys():return False
    for key,a in before.items():
        b=after[key]
        if any(a.get(name,0)!=b.get(name,0) for name in ('symmetry_deg','stl_sha256','mesh_yaw_offset_deg')):return False
        x,y,yaw=a['pose'];u,v,angle=b['pose']
        period=a.get('symmetry_deg',90)
        if type(period) not in (int,float) or not math.isfinite(period) or period<=0:return False
        if any(type(n) not in (int,float) or not math.isfinite(n) for n in (x,y,yaw,u,v,angle)):return False
        # Match the existing metric acquisition consensus, without moving an old
        # path to a new jig or treating a later product stage as an earlier PASS.
        if math.hypot(x-u,y-v)>.5 or angle_delta(yaw,angle,a.get('symmetry_deg',90))>.5:return False
    return True


def retained_jigs(steps,poses):
    """Reuse the original execution basis; current images may show an emptied jig."""
    from so101_teach.episode_inspection import inspection_jig_ids
    required={step['jig_id'] for step in steps if step.get('jig_id')} | inspection_jig_ids(steps)
    if poses is None or required-poses.keys():
        raise ResumeBlocked('기존에 채택한 지그 위치가 없습니다. RESET 후 새 작업을 시작하세요.')
    for key in required:
        pose=poses[key].get('pose');period=poses[key].get('symmetry_deg',90)
        if (not isinstance(pose,(list,tuple)) or len(pose)!=3
                or any(type(n) not in (int,float) or not math.isfinite(n) for n in pose)
                or type(period) not in (int,float) or not math.isfinite(period) or period<=0):
            raise ResumeBlocked('기존 지그 위치가 유효하지 않습니다. RESET 후 새 작업을 시작하세요.')
    return deepcopy(poses)


class FaultRecovery:
    def __init__(self,runner,api,operation,validate=None):
        self.runner=runner;self.api=api;self.operation=operation;self.validate=validate
        self.rechecking=False;self.attempt_generation=None;self.attempt_deadline=None
        self.guards=[];self.last_checked_states=[];self.attempt=0;self.last_error=None;self.resetting=False
    def settings(self):
        expected=self.runner.resume_configuration
        if expected is not None and self.runner.configuration_state()!=expected:
            raise ResumeBlocked('대기 중 모델·보정·ROI·레시피 설정이 변경되었습니다. RESET 후 새 작업을 시작하세요.')
    def observations(self):
        values=[]
        self.runner.state()  # Includes shutdown, peer transport and linear errors.
        for link,guard in self.guards:
            state=link.state();guard.validate(state,self.api.stopped);values.append(state)
        self.last_checked_states=values
        return values
    def check(self):
        control=self.runner.control
        with control.lock:
            control.consume_supervision_stop()
            if control.reset.is_set():self.reset()
            if control.stop_generation!=self.attempt_generation:raise RecheckInterrupted('새 정지 요청으로 재검사 취소')
        values=self.observations()
        if time.monotonic()>=self.attempt_deadline:raise InspectionRetry('재검사 준비 제한시간 초과')
        return values[0] if values else self.runner.state(),False
    def reset(self):
        control=self.runner.control;self.resetting=True
        self.finish_restart('cancelled')
        control.reset.clear();control.resume.clear();control.pause.clear();control.paused=False
        self.runner.deadline=time.monotonic()+120
        raise self.api.ResetRequested('RESET')
    def finish_restart(self,state):
        control=self.runner.control
        with control.lock:
            changed=False
            for receipt in control.receipts.values():
                if receipt['command']=='restart' and receipt['state']=='pending':
                    receipt['state']=state;changed=True
            control.resume.clear()
            if changed:control.save_receipts()
    def record(self,error):
        runner=self.runner;self.last_error=str(error)[:2000]
        runner.failure_context.update(recovery_id=uuid.uuid4().hex,recoverable=True,recovery_attempt=self.attempt)
        runner.failure_images.saved=None
        runner.save_failure_images(error)
        runner.emit('RECOVERY_REQUIRED:'+str(error))
    def run(self,error):
        runner=self.runner;control=runner.control;began=time.monotonic()
        runner.recovery=self
        try:
            control.command('estop')
            self.api.confirm_connected_stop(control,(runner.link,runner.peer),timeout=15.)
            checkpoint=getattr(runner,'inspection_checkpoint',None)
            for link in (runner.link,runner.peer):
                if link is None:continue
                state=link.state()
                if state and state.get('running'):
                    self.guards.append((link,PoseGuard(checkpoint if link is runner.link and checkpoint is not None else state)))
            self.record(error)
            while True:
                control.recovery={'reason':self.last_error,'attempt':self.attempt,'rechecking':False,
                                  'phase':runner.failure_context.get('phase'),
                                  'step_id':runner.failure_context.get('completed_step_id')}
                runner.emit_pause_context();runner.emit('PAUSED');confirmed_generation=None
                while True:
                    runner.state()
                    if runner.linear_active:runner.poll_linear()
                    # Unknown motion/telemetry is fatal; changed but confirmed
                    # stationary poses remain paused and fail the explicit retry.
                    for link,_ in self.guards:
                        if not self.api.stopped(link.state()):raise RuntimeError('보류 중 최신 정지 상태 확인 실패')
                    with control.lock:
                        control.consume_supervision_stop()
                        generation=control.stop_generation
                    if confirmed_generation!=generation:
                        self.api.confirm_connected_stop(control,(runner.link,runner.peer),timeout=15.)
                        confirmed_generation=generation;runner.emit_pause_context()
                    with control.lock:
                        control.consume_supervision_stop()
                        if control.reset.is_set():self.reset()
                        if control.resume.is_set() and confirmed_generation==control.stop_generation:
                            control.resume.clear();control.rechecking=True
                            self.attempt_generation=control.stop_generation;break
                    time.sleep(.05)
                self.attempt+=1;self.rechecking=True
                self.attempt_deadline=time.monotonic()+min(120.,runner.execution_timeout)
                control.recovery={**control.recovery,'attempt':self.attempt,'rechecking':True}
                runner.emit('RECOVERY_RECHECK_STARTED')
                try:
                    self.settings();self.check()
                    if self.validate:self.validate()
                    result=self.operation()
                    self.settings();self.check()
                    with control.lock:
                        control.consume_supervision_stop()
                        if control.reset.is_set():self.reset()
                        if control.shutdown.is_set():raise self.api.ShutdownRequested('PROCESS_SHUTDOWN')
                        if control.stop_generation!=self.attempt_generation:raise RecheckInterrupted('재개 직전 새 정지 요청')
                        completing=[key for key,value in control.receipts.items() if value['command']=='restart' and value['state']=='pending']
                        control.complete('restart')
                        # A delayed receipt write must not authorize motion after
                        # a queued stop; consume it before clearing PAUSED.
                        control.consume_supervision_stop()
                        if control.shutdown.is_set():
                            for key in completing:control.receipts[key]['state']='cancelled'
                            if completing:control.save_receipts()
                            raise self.api.ShutdownRequested('PROCESS_SHUTDOWN')
                        if control.stop_generation!=self.attempt_generation:
                            for key in completing:control.receipts[key]['state']='cancelled'
                            if completing:control.save_receipts()
                            raise RecheckInterrupted('재개 확정 중 새 정지 요청')
                        # Heartbeat/transport errors can arrive while the receipt
                        # is written. Check their cached flags without network I/O
                        # under the stop gate before publishing a successful resume.
                        transport_error=next((source.error for source in (runner.link,runner.peer,runner.linear)
                                              if source is not None and getattr(source,'error',None)),None)
                        if transport_error:
                            for key in completing:control.receipts[key]['state']='failed'
                            if completing:control.save_receipts()
                            raise RuntimeError('재개 확정 중 통신 오류: '+str(transport_error))
                        now=time.monotonic()
                        if now>=self.attempt_deadline or not all(self.api.stopped(state,now=now) for state in self.last_checked_states):
                            for key in completing:control.receipts[key]['state']='failed'
                            if completing:control.save_receipts()
                            raise ResumeBlocked('재개 확정 중 상태 유효 시간이 지났습니다. 새 상태로 다시 재개하세요.')
                        control.pause.clear();control.paused=False
                    runner.emit('RECOVERY_RECHECK_PASSED');runner.emit('RESUMED')
                    runner.failure_images.saved=None
                    for key in ('recovery_id','recoverable','recovery_attempt'):runner.failure_context.pop(key,None)
                    return result
                except RecheckInterrupted:
                    self.finish_restart('cancelled');runner.emit('RECOVERY_RECHECK_CANCELLED')
                except RetryableFault as failure:
                    self.finish_restart('failed');self.record(failure)
                    runner.emit('RECOVERY_BLOCKED:'+str(failure))
                finally:
                    self.rechecking=False;control.rechecking=False
        finally:
            if not self.resetting:runner.deadline+=time.monotonic()-began
            runner.recovery=None;control.rechecking=False;control.recovery=None
