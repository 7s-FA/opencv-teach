"""Bounded endpoint position-error correction, not model-based gravity torque.

Only explicit moves may use this controller. Original goals stay immutable.
"""
JOINTS=('shoulder_lift','elbow_flex')
MAX_BIAS=64
BIAS_RATE=40.  # ticks/second; no catch-up after a delayed sample
RESIDUAL=20
STATIONARY_SECONDS=.10
FINE_BIAS_RATE=12.
STALL_COMMAND_TICKS=20
STALL_RESPONSE_SECONDS=.25
MAX_LOAD=409  # controller indication, NOT a calibrated torque measurement

class CorrectionStalled(ValueError):
    """The servo received correction but the measured error stopped improving."""

class LoadCompensation:
    def __init__(self):self.reset()
    def reset(self):self.states={};self.waiting={};self.last=None
    def command(self,goal,snapshot,calibration,now,*,enabled,prepare_goal=None):
        dt=0. if self.last is None else max(0.,min(.05,now-self.last))
        self.last=now;out=dict(goal)
        for n in JOINTS:
            final=goal if enabled or prepare_goal is None else prepare_goal
            actual=snapshot.ticks[n];h=snapshot.telemetry[n];error=final[n]-actual
            st=self.states.get(n);raw=h.get('load_raw');velocity=h.get('velocity_signed_raw')
            valid=(snapshot.fresh(now) and snapshot.calibration_matches and h.get('torque')==1
                   and h.get('status')==0 and h.get('packet_alarm',0)==0
                   and type(raw) is int and 0<=raw<=2047 and (raw&1023)<=MAX_LOAD
                   and h.get('temperature_c',100)<60 and velocity is not None)
            if not enabled:
                # Observe only a stopped joint at the very end of deceleration.
                # This never adds a command before the nominal trajectory ends.
                if (prepare_goal is not None and abs(goal[n]-final[n])<=2 and valid
                        and RESIDUAL<abs(error)<=80 and abs(velocity)<=5):
                    self.observe_stationary(n,actual,final[n],now)
                else:self.waiting.pop(n,None)
                continue
            if st is None:
                if not valid or not RESIDUAL<abs(error)<=80 or abs(velocity)>5:
                    self.waiting.pop(n,None);continue
                since=self.observe_stationary(n,actual,goal[n],now)
                if now-since<STATIONARY_SECONDS:continue
                st={'bias':0.,'sign':1 if error>0 else -1,'progress_at':now,'position':actual,
                    'best_error':abs(error),'command_anchor':h.get('goal_ticks',goal[n]),'effort_at':None}
                self.states[n]=st
            if not valid:raise ValueError(n+': 부하 보정 중 상태 확인 불가 · 추가 보정 중지')
            sent=h.get('goal_ticks',goal[n])
            # Only smaller target error counts as progress; vibration/drift does not.
            if st['best_error']-abs(error)>=2:
                st.update(progress_at=now,position=actual,best_error=abs(error),command_anchor=sent,effort_at=None)
            delivered=abs(sent-st['command_anchor'])
            st['delivered_since_progress']=delivered
            if delivered>=STALL_COMMAND_TICKS:
                if st['effort_at'] is None:st['effort_at']=now
            else:st['effort_at']=None
            if (abs(error)>RESIDUAL and now-st['progress_at']>=1.
                    and st['effort_at'] is not None and now-st['effort_at']>=STALL_RESPONSE_SECONDS):
                raise CorrectionStalled(n+f': 추가 {delivered}틱 보정 후에도 목표 오차 감소 없음 · 추가 보정 중지')
            # Stop integrating near the original goal; unwind on overshoot.
            rate=min(BIAS_RATE,max(FINE_BIAS_RATE,2.*(abs(error)-RESIDUAL)))
            change=0. if abs(error)<=RESIDUAL else (1 if error>0 else -1)*rate*dt
            st['bias']=max(-MAX_BIAS,min(MAX_BIAS,st['bias']+change))
            if st['bias']*st['sign']<0:st['bias']=0.
            m=calibration.motors[n];out[n]=max(m.low+8,min(m.high-8,round(goal[n]+st['bias'])))
            st['bias']=out[n]-goal[n] if out[n] in (m.low+8,m.high-8) else st['bias']
            if abs(error)>RESIDUAL and (abs(st['bias'])>=MAX_BIAS or out[n] in (m.low+8,m.high-8)):
                raise ValueError(n+': 부하 보정 한도 도달 · 원래 목표 미도달')
        return out
    def observe_stationary(self,name,actual,goal,now):
        previous=self.waiting.get(name)
        if previous is None or previous[2]!=goal or abs(actual-previous[1])>2:
            self.waiting[name]=(now,actual,goal)
        return self.waiting[name][0]
    def tolerance(self,name,ordinary):return RESIDUAL if name in JOINTS else ordinary
    def evidence(self):return {n:dict(s) for n,s in self.states.items()}
