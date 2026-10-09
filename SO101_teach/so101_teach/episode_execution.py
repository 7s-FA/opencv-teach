"""PC motion continues while fresh-frame visual inspections run."""
from copy import deepcopy
import time
from .domain import atomic_json
from .episode_inspection import inspection_schedule,InspectionDecision,startup_steps,GroupInspectionDecision
from .episode_inspection_runtime import FrameInspection,require_resources,InspectionBatch,FrameInspections


class InspectedExecution:
    def __init__(self,app,steps,targets,*,fixed_jigs=None):
        require_resources();self.app=app;self.session=app.session;self.episode_id=app.episode['id'];self.product=app.episode['product_type']
        self.steps=deepcopy(steps);self.targets=deepcopy(targets);self.scheduled=inspection_schedule(steps);self.started_checks=set();self.index=0;self.phase='ready';self.request_id=None;self.worker=None;self.decision=None;self.submitted_at=None;self.error=None;self.dispatching=False
        self.fixed_jigs=deepcopy(fixed_jigs or {})
        from .episode_inspection import inspection_jig_ids
        if inspection_jig_ids(steps)-self.fixed_jigs.keys():raise ValueError('실행 전 안착 검사 지그 위치를 먼저 확정하세요.')
        self.path=app.data_dir/'diagnostics'/f'episode-inspection-{time.time_ns()}.json'
        from .assembly_evidence import AssemblyEvidence,geometry_scope
        self.assembly_evidence=AssemblyEvidence(app.data_dir/'assembly-evidence.json',scope=geometry_scope(app.workcell_preview),
            episode_id=self.episode_id)
        self.batch=InspectionBatch(self.product,self.make_worker,self.passed,evidence=self.assembly_evidence)
        self.record={'episode_id':self.episode_id,'product_type':self.product,'started_at':time.time(),'checks':[],'state':'prepared'};self.save()
    @property
    def busy(self):return self.phase in ('ready','moving','inspection')
    def save(self):atomic_json(self.path,self.record)
    def dispatch(self):
        self.dispatching=True
        try:self.request_id=self.app.motion_request('play',self.targets)
        finally:self.dispatching=False
        self.phase='moving';return self.request_id
    def make_worker(self,step):
        a=self.app;a.start_camera();a.notice('안착 검사 중 · '+step['name']+' · 다음 동작 계속')
        from .episode_inspection_runtime import read_initial_linear_state
        reader=(lambda:read_initial_linear_state(a.data_dir)) if step['inspection']['station']=='linear' else None
        return FrameInspection(a.data_dir,a.profile,a.catalog,a.workcell_preview,step['inspection'],self.product,linear_state_reader=reader,fixed_jigs=self.fixed_jigs)
    def passed(self,step):
        self.record['checks'].append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})});self.save()
    def close_worker(self):
        self.batch.close()
        if self.worker:self.worker.close();self.worker=None
    def cancel(self,reason='사용자 정지',*,hold=False):
        if not self.busy:return
        self.phase='failed';self.error=reason
        if hold:
            try:self.session.request('hold')
            except Exception as exc:self.error+=' · 정지 요청 확인 실패: '+str(exc)
        self.close_worker();self.record.update(state='stopped',error=self.error,finished_at=time.time())
        try:self.save()
        except OSError as exc:self.error+=' · 기록 저장 실패: '+str(exc)
        if not self.app.camera_needed():self.app.suspend_camera()
        self.app.notice('에피소드 검사 정지 · '+self.error,True)
    def poll(self):
        if not self.busy:return
        a=self.app;s=self.session
        try:
            if a.session is not s or not s.running or s.error:raise ValueError('팔로워 연결 변경·오류')
            if a.episode['id']!=self.episode_id:raise ValueError('실행 중 에피소드가 변경됨')
            if not a.latest or not a.latest.fresh() or not a.latest.calibration_matches:raise ValueError('실행 중 최신 자세 확인 실패')
            if s.state not in ('MOVING','HOLD'):raise ValueError('실행 중 팔로워 상태 변경')
            complete=s.completed_request_id==self.request_id and s.state=='HOLD'
            active=s.active_request_id==self.request_id
            if self.phase=='moving' and active and s.state=='HOLD' and not complete and not s.command_pending.is_set() and not s.program_active.is_set():raise ValueError('드라이버 보호 정지')
            count=len(self.steps) if complete else getattr(s,'index',0) if active else 0
            if type(count) is not int or not 0<=count<=len(self.steps):raise ValueError('실행 스텝 번호 오류')
            for after,step in self.scheduled:
                if count>=after and step['id'] not in self.started_checks:
                    self.batch.start(step);self.started_checks.add(step['id'])
            if self.batch.pending:
                if a.camera and getattr(a.camera,'error',None):raise ValueError('카메라 오류: '+a.camera.error)
                view=a.camera_view_observation() if a.camera else None
                self.batch.poll((view[0],view[2]) if view else None)
            if complete:
                if self.batch.pending:self.phase='inspection'
                else:self.advance()
        except Exception as exc:self.cancel(str(exc),hold=True)
    def advance(self):
        self.record.update(state='done',finished_at=time.time());self.save();self.assembly_evidence.commit();self.phase='done'
        if not self.app.camera_needed():self.app.suspend_camera()
        self.app.notice('에피소드 완료 · 지정한 안착 검사 모두 정상')


class StartupInspectionRun(InspectedExecution):
    """No movement request is sent until every initial condition has passed."""
    def __init__(self,app,continuation,*,fixed_jigs=None):
        super().__init__(app,[],[],fixed_jigs=fixed_jigs);self.checks=startup_steps(app.episode);self.continuation=continuation
    def start(self):
        try:
            self.require_hold();self.begin_check()
        except Exception as exc:self.cancel(str(exc),hold=True)
    def require_hold(self):
        a=self.app;s=self.session
        if (a.session is not s or not s or not s.running or s.error or s.state!='HOLD'
            or s.command_pending.is_set() or s.program_active.is_set()
            or not a.latest or not a.latest.fresh() or not a.latest.calibration_matches):
            raise ValueError('시작 전 검사에는 팔로워의 최신 자세 유지 상태가 필요합니다.')
        if a.episode['id']!=self.episode_id:raise ValueError('시작 전 검사 중 에피소드가 변경됨')
    def begin_check(self):
        self.phase='inspection';self.decision=GroupInspectionDecision(self.checks,time.monotonic(),evidence=self.assembly_evidence);self.submitted_at=None
        from .episode_inspection_runtime import read_initial_linear_state
        reader=(lambda:read_initial_linear_state(self.app.data_dir)) if any(s['inspection']['station']=='linear' for s in self.checks) else None
        self.worker=FrameInspections(self.app.data_dir,self.app.profile,self.app.catalog,self.app.workcell_preview,self.checks,linear_state_reader=reader,fixed_jigs=self.fixed_jigs)
        self.app.start_camera();self.app.notice(f'시작 전 동시 검사 · {len(self.checks)}개 대상 · 이동 대기')
    def poll(self):
        if not self.busy:return
        try:
            self.require_hold();a=self.app;now=time.monotonic()
            if a.camera and getattr(a.camera,'error',None):raise ValueError('카메라 오류: '+a.camera.error)
            view=a.camera_view_observation() if a.camera else None
            if view and now-view[2]<1 and view[2]<=now and view[2]!=self.submitted_at:
                self.worker.submit(view[0],view[2]);self.submitted_at=view[2]
            passed=self.decision.observe(self.worker.result,now)
            for step in passed:
                self.record['checks'].append({'step_id':step['id'],'step_name':step['name'],'check':deepcopy(step['inspection']),'product':step['inspection_product'],'result':'PASS','at':time.time(),**({'evidence':deepcopy(step['inspection_basis'])} if step.get('inspection_basis') else {})})
            if passed:self.save()
            self.worker.retain(self.decision.pending)
            if self.decision.pending:
                counts=' · '.join(self.decision.steps[key]['name']+f" {d.count}/{d.check.get('minimum_observations',2)}" for key,d in self.decision.pending.items())
                a.notice('시작 전 동시 검사 · '+counts+' · 이동 대기');return
            self.close_worker()
            self.record.update(state='passed',finished_at=time.time());self.save();self.phase='done'
            a.inspection_run=None;self.continuation()
        except Exception as exc:
            # A continuation failure is still a stopped start, never a silent pass.
            if self.phase=='done':self.phase='ready'
            self.cancel(str(exc),hold=True)
