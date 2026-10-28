"""Monotonic job/phase timing, shared across a build/load status handoff."""
from copy import deepcopy
import time
import uuid
import unicodedata


class JobTiming:
    def __init__(self):
        self.job_id=uuid.uuid4().hex
        self.started=time.monotonic();self.previous=self.started
        self.phase=None;self.phase_started=self.started;self.phases=[];self.finished=None
        self.mode=None

    def begin(self,command,*,started=None):
        if started is not None:self.started=self.previous=self.phase_started=started
        self.mode=command
        self.phase=('build_prepare' if command.startswith('build') else
                    'load_prepare' if command.startswith('load') else 'preparation')

    def transition(self,phase,now,*,completed=True):
        if self.phase==phase:return
        if self.phase is not None:
            self.phases.append({'phase':self.phase,'seconds':round(max(0.,now-self.phase_started),3),
                                'completed':completed})
        self.phase=phase;self.phase_started=now

    def record(self,arm,suffix):
        now=time.monotonic()
        elapsed=round(max(0.,now-self.started),3)
        delta=round(max(0.,now-self.previous),3);self.previous=now
        if self.finished is None:
            if suffix=='RESET_STARTED' or suffix.startswith('ARM_RESET:') and suffix.endswith(':RESET_STARTED'):
                self.transition('recovery',now,completed=self.phase in ('preparation','recovery'))
            elif suffix=='EPISODE_STARTED':self.transition('build' if arm=='arm2' else 'load',now)
            elif suffix=='BUILD_DONE':self.transition('load_prepare',now)
            elif suffix.startswith('CHAIN_LOAD_STARTED:') and self.phase!='load_prepare':self.transition('load_prepare',now)
            elif suffix in ('DONE','CHAIN_DONE','RESET_DONE') or suffix.startswith(('FAILED:','REJECTED:')):
                completed=not suffix.startswith(('FAILED:','REJECTED:'))
                self.transition(None,now,completed=completed)
                self.finished={'job_id':self.job_id,'total_seconds':elapsed,'completed':completed,
                               'phases':deepcopy(self.phases),
                               'includes':'검사·리니어 대기·단계 정리·일시정지 포함'}
        return {'job_id':self.job_id,'elapsed_s':elapsed,'since_previous_s':delta,
                **({'timing_summary':deepcopy(self.finished)} if self.finished is not None else {})}


LABELS={'recovery':'안전 복귀','build_prepare':'조립 전 준비','build':'조립','load_prepare':'하차 준비','load':'하차','preparation':'준비·복귀'}


def summary_lines(summary):
    if not summary:return []
    title='작업 소요시간' if summary['completed'] else '중단 시점까지 소요시간'
    rows=[f"{LABELS.get(p['phase'],p['phase'])} {p['seconds']:.3f}초"+
          ('' if p['completed'] else ' (중단)') for p in summary['phases']]
    rows.append(f"전체 {summary['total_seconds']:.3f}초")
    return [title+': '+' / '.join(rows),summary.get('includes','')]


def preparation_lines(profile,arm):
    metrics=profile.get('metrics',{})
    groups=[('초기화·팔 준비',[('load.total','설정 로딩'),('load.model_verify','모델 파일 검증'),
              ('link.ensure','실행부 확인'),('follower.configure','설정 반영'),('follower.connect','팔 연결'),
              ('torque.ready','토크 ON 확인'),('camera.before_model_warmup','카메라 준비→모델 시작'),
              ('camera.model_warmup','카메라 모델 기동·워밍업'),('camera.ready_wait','카메라 추가 대기')]),
            ('지그·소비 프레임 누계',[('jig.processing_seconds','처리 전체'),('jig.model_seconds','AI'),('jig.opencv_seconds','OpenCV 정합')]),
            ('시작 검사',[('inspection.model_warmup','모델 준비'),('inspection.model_ready_wait','모델 준비 대기'),
              ('inspection.model_lock_wait','모델 사용 대기'),('inspection.geometry_prepare','기하 준비'),
              ('inspection.frame_queue_wait','프레임 큐 대기'),('inspection.anchors','검사 영역 구성'),
              ('inspection.geometry','형상 판정'),('inspection.ai','AI'),('inspection.combine','결과 결합')]),
            ('검사 후 정리·경로',[('inspection.close','검사 정리'),('camera.pause','카메라 전환'),
              ('planning.rpc','경로 계산 RPC 전체'),('planning.wait_after_preparation','준비 후 계산 대기')])]
    # Korean occupies two terminal cells; str.ljust() counts code points only.
    def cells(text):
        return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in text)
    width=max(cells(title) for _,keys in groups for _,title in keys)
    lines=['항목'+' '*(width-cells('항목'))+'  누계(초)   횟수']
    for label,keys in groups:
        rows=[]
        for key,title in keys:
            row=metrics.get(arm+'.'+key)
            if row:rows.append(title+' '*(width-cells(title))+f"  {row['total_seconds']:8.3f}  {row['count']:5d}")
        if rows:lines.extend(['['+label+']',*rows])
    lines.extend(['병렬·포함 구간은 겹치므로 합산하지 않음.',
                  '평균·최대·시작/완료 시점과 원문: work_watch --details'])
    return lines
