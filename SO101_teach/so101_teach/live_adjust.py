"""Explicit opt-in editing: one move in flight, latest bounded draft only."""
import time

class LiveAdjust:
    def __init__(self,app):
        self.app=app;self.owner=None;self.dispatching=False;self.request_id=None;self.waiting=False
        self.sent={};self.pending=False;self.context=None
        self.feedback_wait_at=None
    def identity(self,owner):
        return (self.app.episode['id'],owner.selected,bool(owner.follow_jig.get()),owner.selected_jig_id())
    def toggle(self,owner):
        if self.owner is not None:
            same=self.owner is owner;self.stop()
            if same:return
        a=self.app;s=a.session
        if not s or not s.running or s.state!='HOLD' or s.command_pending.is_set():
            raise ValueError('현재 자세 유지 상태에서 실물 조정을 켜세요.')
        if not a.latest or not a.latest.fresh() or not a.latest.calibration_matches:
            raise ValueError('최신 실물 틱과 영점을 확인한 뒤 실물 조정을 켜세요.')
        # Initial explicit move uses the complete editor pose; partial entry is
        # normalized before arming the toggle. Later edits are bounded commits.
        owner.apply_target(owner.calibration.ticks({n:int(v.get()) for n,v in owner.tick_vars.items()}))
        self.owner=owner;self.context=self.identity(owner);self.sent=owner.target.copy();self.pending=True
        self.feedback_wait_at=None
        self.update_controls();self.dispatch()
    def stop(self,*,halt=True):
        if self.owner is None:return
        a=self.app;s=a.session;moving=self.request_id is not None and s and s.running and s.completed_request_id!=self.request_id
        self.owner=None;self.pending=False;self.waiting=False;self.request_id=None
        self.feedback_wait_at=None
        try:
            if halt and moving and not getattr(getattr(a,'remote',None),'error',None):a.motion_request('hold')
        finally:
            try:a.stop_preview(quiet=True)
            finally:self.update_controls()
    def edit(self,owner,name,value):
        if self.owner is None:return False
        if self.owner is not owner:self.stop();return False
        if self.identity(owner)!=self.context:self.stop();return False
        m=owner.calibration.motors[name];base=self.sent[name]
        value=max(m.low,min(m.high,max(base-50,min(base+50,int(value)))))
        owner.apply_target({**owner.target,name:value})
        self.pending=owner.target!=self.sent;self.app.last_render=None
        self.dispatch();return True
    def submitted(self,request_id):
        if self.owner is not None:self.request_id=request_id;self.waiting=False
    def dispatch(self):
        if self.owner is None or self.waiting or self.request_id is not None or not self.pending:return
        s=self.app.session
        if not s or s.state!='HOLD' or s.command_pending.is_set():return
        sample=getattr(s,'latest',None) or self.app.latest
        if not sample or not sample.fresh() or not sample.calibration_matches:return
        self.owner.apply_target(self.owner.target)
        self.sent=self.owner.target.copy();self.pending=False;self.waiting=True;self.dispatching=True
        try:self.owner.execute_target()
        except Exception:
            self.stop();raise
        finally:self.dispatching=False
    def poll(self):
        if self.owner is not None:
            a=self.app;s=a.session
            sample=getattr(s,'latest',None) or a.latest
            if (a.closed or not s or not s.running or getattr(s,'error',None) or
                getattr(getattr(a,'remote',None),'error',None) or self.identity(self.owner)!=self.context or
                s.state not in ('HOLD','MOVING') or sample and not sample.calibration_matches):
                self.stop(halt=bool(s and s.running));return
            if not sample or not sample.fresh():
                now=time.monotonic()
                if self.feedback_wait_at is None:
                    self.feedback_wait_at=now
                    a.notice('실물값 수신 대기 · 실물 조정 유지 · 새 조작 전송 대기')
                age=now-sample.monotonic if sample else now-self.feedback_wait_at
                if age>=2. or age<0:
                    self.stop(halt=True);a.notice('실물값 수신 중단 · 실물 조정 종료',True)
                return
            if self.feedback_wait_at is not None:
                self.feedback_wait_at=None;a.notice('실물값 수신 복구 · 실물 조정 계속')
            if self.request_id is not None:
                if s.completed_request_id==self.request_id:self.request_id=None
                elif not s.command_pending.is_set() and not s.program_active.is_set():self.stop();return
            if self.waiting and not a.pending_execution and not a.remote_plan_job:
                self.stop();return
            self.dispatch()
        self.update_controls()
    def update_controls(self):
        for editor in (self.app,getattr(self.app,'second_editor',None)):
            if editor is None or not hasattr(editor,'move_btn'):continue
            active=self.owner is editor
            text='● 실물 조정 끄기' if active else '실물 조정 켜기'
            style='Compact.Primary.TButton' if active else 'Compact.TButton'
            if str(editor.move_btn.cget('text'))!=text or str(editor.move_btn.cget('style'))!=style:
                editor.move_btn.configure(text=text,style=style)
            if active and editor.move_btn.instate(['disabled']):editor.move_btn.state(['!disabled'])
            for slider in editor.sliders.values():
                if slider.instate(['disabled'])!=active:slider.state(['disabled'] if active else ['!disabled'])
