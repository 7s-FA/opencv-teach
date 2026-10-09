"""Bind leader assistance only to an explicit follow intent; never to connect."""
import time
from .leader_assist import LEVELS

CHOICES=('사용 안 함',*LEVELS)

class LeaderAssistControls:
    def save_leader_assist(self):
        choice=self.leader_assist_choice.get()
        if choice not in CHOICES:raise ValueError('리더 보조 강도를 선택하세요.')
        leader=self.leader_session
        if choice!='사용 안 함' and (getattr(self.session,'state',None)=='FOLLOW' or getattr(leader,'assist_state',None) in ('WAITING','STARTING','ACTIVE')):
            self.leader_assist_choice.set(self.preferences.get('leader_gravity_assist','약하게'))
            raise ValueError('따라가기를 정지한 뒤 리더 보조 강도를 변경하세요.')
        if choice=='사용 안 함':self.stop_leader_assist()
        self.preferences['leader_gravity_assist']=choice;self.save_preferences()
        self.notice('리더 무게 보조 '+choice+' · 다음 리더 따라가기에 적용' if choice!='사용 안 함' else '리더 무게 보조 해제 요청')
    def stop_leader_assist(self):
        leader=self.leader_session
        if leader and callable(getattr(leader,'stop_assist',None)):leader.stop_assist()
    def prepare_leader_assist(self):
        choice=self.preferences.get('leader_gravity_assist','약하게')
        if choice=='사용 안 함':return
        if choice not in LEVELS:raise ValueError('설정·안내 → 로봇에서 리더 보조 설정을 확인하세요.')
        leader=self.leader_session;session=self.session
        if not leader or not callable(getattr(leader,'begin_assist',None)):
            raise ValueError('리더 무게 보조를 위해 로봇 연결을 해제한 뒤 다시 연결하세요.')
        def guard():
            if self.closed or self.session is not session or self.leader_session is not leader or not session.running:return 'STOP'
            if not 0<=time.monotonic()-self.ui_heartbeat<=.35:return 'DELAY'
            state=getattr(session,'state',None)
            if state not in ('FOLLOW','HOLD'):return 'STOP'
            sample=session.latest
            if sample and not sample.calibration_matches:return 'LOST'
            if not sample or not sample.fresh():return 'DELAY'
            return 'RUN' if state=='FOLLOW' else 'WAIT'
        leader.begin_assist(choice,guard,self.reference.radians)
        self.last_assist_fault=None
    def poll_leader_assist(self):
        leader=self.leader_session;state=getattr(leader,'assist_state','OFF');error=getattr(leader,'assist_error',None)
        selected=self.preferences.get('leader_gravity_assist','약하게')
        label=('작동 중 · '+str(getattr(leader,'assist_level','약하게')) if state=='ACTIVE' else
               '시작 확인 중' if state in ('WAITING','STARTING') else
               '중단 · 리더 다시 연결 필요' if error else '사용 안 함' if selected=='사용 안 함' else
               '수신 지연으로 해제 · 읽기 유지' if getattr(leader,'assist_interruption',None) else '대기 · 따라가기에서 '+selected)
        if self.leader_assist_status.get()!=label:self.leader_assist_status.set(label)
        base=self.connection.get().split(' · 리더 보조 ')[0]
        text=base+' · 리더 보조 '+str(leader.assist_level) if state=='ACTIVE' else base
        if self.connection.get()!=text:self.connection.set(text)
        if error and getattr(self.session,'state',None)=='FOLLOW' and getattr(self,'last_assist_fault',None)!=(id(leader),error):
            self.last_assist_fault=(id(leader),error)
            try:self.motion_request('hold')
            finally:self.notice('리더 보조 중단 · 따라가기 정지: '+error,True)
