"""Confirm sustained resistance with stalled travel, not a moving load spike."""
import time
DEFAULT_CONTACT_LOAD=80  # Raw controller load magnitude; provisional, not Newtons.
OPEN_RELEASE_TICKS=8
CONTACT_SECONDS=.12
CONTACT_SAMPLES=3
CONTACT_STILL_TICKS=3
CONTACT_MAX_GAP=.15

class GripperContact:
    def __init__(self,threshold=DEFAULT_CONTACT_LOAD):
        if type(threshold) is not int or not 1<=threshold<=1023:raise ValueError('집게 접촉 부하는 1~1023의 정수로 입력하세요.')
        self.threshold=threshold;self.reset()
    def reset(self):self.hold_tick=None;self.pending=None
    def constrain(self,target,*,opening_intent=False,limit_pending=True):
        if self.hold_tick is None:
            if self.pending is None:return target
            actual=self.pending['actual']
            if opening_intent and target>actual+OPEN_RELEASE_TICKS:
                self.pending=None;return target
            if not limit_pending:return target
            return max(target,self.pending['initial_goal'])
        if opening_intent and target>self.hold_tick+OPEN_RELEASE_TICKS:
            self.reset();return target
        return max(target,self.hold_tick)
    def candidate(self,actual,requested,health):
        if self.hold_tick is not None or requested>=actual or health.get('torque')!=1:return False
        # A future endpoint is not evidence that the servo is already closing.
        if health.get('goal_ticks',requested)>=actual:return False
        raw=health.get('load_raw')
        if raw is None:return False
        # Feetech STS Present_Load is sign-magnitude, with sign bit 10.
        if type(raw) is not int or not 0<=raw<=2047:raise ValueError('집게 부하값 형식 오류')
        return (raw&1023)>=self.threshold
    def observe(self,actual,requested,health,*,now=None):
        now=time.monotonic() if now is None else now
        triggered=self.candidate(actual,requested,health);p=self.pending
        goal=health.get('goal_ticks',requested);raw=health.get('load_raw')
        # Every confirmation sample must meet the selected load threshold.
        # A transient spike or deliberately reduced pressure is not contact.
        if not triggered:self.pending=None;return None
        if (p is None or now<=p['last'] or now-p['last']>CONTACT_MAX_GAP
                or max(p['high'],actual)-min(p['low'],actual)>CONTACT_STILL_TICKS):
            p={'since':now,'last':now,'samples':0,'low':actual,'high':actual,
               'initial_goal':goal,'trigger_load_magnitude':raw&1023}
        p.update(actual=actual,last=now,samples=p['samples']+1,low=min(p['low'],actual),high=max(p['high'],actual))
        self.pending=p
        if now-p['since']<CONTACT_SECONDS or p['samples']<CONTACT_SAMPLES:return None
        self.hold_tick=actual
        self.pending=None
        return {'hold_tick':actual,'requested_tick':requested,'load_raw':health['load_raw'],'load_magnitude':health['load_raw']&1023,
                'threshold':self.threshold,'confirmed_seconds':now-p['since'],'confirmed_samples':p['samples'],
                'trigger_load_magnitude':p['trigger_load_magnitude'],'confirmation_rule':'continuous_threshold',
                'position_spread_ticks':p['high']-p['low'],'servo_goal_ticks':health.get('goal_ticks')}
