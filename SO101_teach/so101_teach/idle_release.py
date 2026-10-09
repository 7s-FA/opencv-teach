"""A cancellable idle release owned by the runtime, never by a finished job."""

import threading

class IdleRelease:
    def __init__(self):self.lock=threading.RLock();self.cancel()
    def cancel(self):
        with self.lock:self.session=None;self.since=None;self.baseline=None
    def schedule(self,session,seconds=3.):
        with self.lock:
            self.cancel()
            if not session or not session.running:raise ValueError('연결된 팔로워 없음')
            if session.state=='READ_ONLY':return
            if session.state!='HOLD' or session.program_active.is_set() or session.command_pending.is_set():raise ValueError('정지 완료 후 토크 해제를 예약하세요.')
            self.session=session;self.seconds=seconds
    def poll(self,current,now):
        with self.lock:
            s=self.session
            if s is None:return False
            if current is not s or not s.running or s.error or s.state!='HOLD' or s.program_active.is_set() or s.command_pending.is_set():
                self.cancel();return False
            sample=s.latest
            if (not sample or not sample.fresh(now) or not sample.calibration_matches or len(sample.ticks)!=6
                    or len(sample.telemetry)!=6 or any(h.get('moving')!=0 for h in sample.telemetry.values())):
                self.since=None;self.baseline=None;return False
            ticks=sample.ticks;at=sample.monotonic
            if self.baseline is None or ticks.keys()!=self.baseline.keys() or any(abs(ticks[k]-self.baseline[k])>2 for k in ticks) or at<self.since:
                self.baseline=dict(ticks);self.since=at
            elif at-self.since>=self.seconds:
                self.cancel();s.request('release');return True
            return False
