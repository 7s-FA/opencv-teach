"""Explain saved inspection failures without guessing from the last done step.

Desktop-only: reads the existing archive payload; never queries a controller.
"""
from datetime import datetime
import math


def number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>0 else None


def timestamp(value):
    value=number(value)
    if value is None:return '미확인'
    try:return datetime.fromtimestamp(value).strftime('%m-%d %H:%M:%S.%f')[:-3]
    except (ValueError,OSError,OverflowError):return '미확인'


def event_parts(row):
    head,_,detail=str(row.get('status','')).partition(':')
    return head.partition('_')[2],detail


def done_step(row):
    kind,detail=event_parts(row)
    if kind!='STEP_DONE':return None
    progress,_,name=detail.partition(':')
    step=(row.get('event') or {}).get('step') or {}
    return {'id':step.get('step_id'),'name':step.get('step_name') or name,
            'progress':progress if '/' in progress else None,'at':row.get('at')}


def step_text(step):
    if not step or not step.get('name'):return '미확인'
    return step['name']+(f" (실행 {step['progress']})" if step.get('progress') else '')


def summarize(payload):
    doc=payload.get('record') or {};run=payload.get('run') or {}
    context=doc.get('context') or {};analysis=run.get('analysis') or {}
    run_id=context.get('run_id') or run.get('run_id')
    arm=context.get('arm')
    events=[e for e in run.get('events',[]) if isinstance(e,dict)
            and (not run_id or e.get('run_id',run_id)==run_id)
            and (not arm or e.get('arm',arm)==arm)]
    saved_at=number(doc.get('failed_at'))
    if saved_at is not None:events=[e for e in events if number(e.get('at')) is None or e['at']<=saved_at]
    terminal=next((e for e in reversed(events) if event_parts(e)[0] in ('FAILED','REJECTED')),None)
    reason=str(doc.get('error') or (event_parts(terminal)[1] if terminal else ''))
    failure=next((e for e in reversed(events) if event_parts(e)[0]=='INSPECTION_FAILED'
                  and (not reason or event_parts(e)[1]==reason or event_parts(e)[1].endswith(':'+reason))),None)
    if failure:
        events=events[:events.index(failure)+1]
        arm=arm or failure.get('arm')
    # A general motion error after an earlier successful inspection is not a
    # new inspection failure. Only explicit failure evidence participates.
    inspection=doc.get('inspection') or {}
    specified=[s for s in inspection.get('steps',[]) if isinstance(s,dict)]
    if isinstance(inspection.get('step'),dict):specified=[inspection['step']]
    reason=reason or (event_parts(failure)[1] if failure else '')
    starts=[]
    for e in events:
        if event_parts(e)[0]!='INSPECTION_STARTED':continue
        key,_,name=event_parts(e)[1].partition(':')
        starts.append({'id':key,'name':name,'at':number(e.get('at'))})
    def matches(step):
        return any(reason.startswith(str(step.get(k)) + separator)
                   for k in ('id','name') if step.get(k) for separator in (':',' ·','：'))
    candidates=[s for s in specified if matches(s)]
    selected=candidates[0] if len(candidates)==1 else specified[0] if len(specified)==1 else None
    if selected is None and failure:
        matches_by_id={s['id']:s for s in starts if matches(s)}
        if len(matches_by_id)==1:selected=next(iter(matches_by_id.values()))
    start=next((s for s in reversed(starts) if selected and s['id']==selected.get('id')),None)
    started=start.get('at') if start else None
    completed=None;completion_source=None
    log=payload.get('inspection_log') or {}
    if not context.get('episode_id') or log.get('episode_id',context['episode_id'])==context['episode_id']:
        for check in log.get('checks',[]):
            at=number(check.get('at'))
            if check.get('result')!='FAIL' or at is None:continue
            if started is not None and at<started:continue
            if saved_at is not None and at>saved_at:continue
            same_error=str(check.get('error',''))==reason and bool(reason)
            same_step=selected and check.get('step_id')==selected.get('id') and bool(selected.get('id'))
            if same_error or same_step:completed=at;completion_source='검사 기록'
    if completed is None and failure:
        completed=number(failure.get('at'));completion_source='실패 알림'
    cutoff=number(failure.get('at')) if failure else completed or saved_at
    done=[s for e in events if (s:=done_step(e)) and (cutoff is None or number(s['at']) is not None and s['at']<=cutoff)]
    last=done[-1] if done else None
    if context.get('completed_step_name'):
        matching=[s for s in done if s['id']==context.get('completed_step_id') or s['name']==context['completed_step_name']]
        last=matching[-1] if matching else {'id':context.get('completed_step_id'),'name':context['completed_step_name']}
    designated=dict(selected) if selected else None
    if designated:
        matching=[s for s in done if s.get('id') and s['id']==designated.get('id')]
        if not matching:
            matching=[s for s in done if s['name']==designated.get('name') and (started is None or number(s['at']) is not None and s['at']<=started)]
        if matching:designated['progress']=matching[-1].get('progress')
    progress,received=observed_step(context,analysis,failure,cutoff,arm)
    duration=completed-started if completed is not None and started is not None and completed>=started else None
    return {'inspection_step':step_text(designated),'interrupted_step':step_text(progress),
            'last_completed_step':step_text(last),'started_at':started,'completed_at':completed,
            'completion_source':completion_source,'duration_seconds':duration,'received_at':received,
            'stop_note':('중단 시 진행 스텝: 마지막 수신 '+timestamp(received)+' 기준 · 정지 확정 상태 미기록'
                         if progress else '중단 시 진행 스텝 미확인 · 마지막 완료 스텝과 구분')}


def observed_step(context,analysis,failure,cutoff,arm):
    state=context.get('last_follower_state') or {}
    received=number((state.get('latest') or {}).get('wall_time'))
    index=state.get('index');request=state.get('request_id')
    if (state.get('state')!='MOVING' or not state.get('program_active') or
            type(index) is not int or index<0 or request is None or received is None or
            cutoff is None or not 0<=cutoff-received<=2):return None,received
    evidence=(failure or {}).get('event') or {}
    plan_id=evidence.get('plan_id');context_id=evidence.get('context_id')
    # The index belongs to a request, not the episode. A resumed/chunked
    # request needs an explicit offset; old archives do not always save it.
    if not plan_id or not context_id or not arm:return None,received
    motions=[m for m in analysis.get('motion_calls',[]) if m.get('arm')==arm and m.get('action')=='play'
             and m.get('plan_id')==plan_id and m.get('context_id')==context_id]
    if not motions:return None,received
    motion=motions[-1];requests=motion.get('requests') or []
    if len(requests)!=1 or requests[0].get('request_id')!=request:return None,received
    plans=[p for p in analysis.get('plans',[]) if p.get('plan_id')==plan_id and p.get('arm')==arm and p.get('context_id')==context_id]
    if len(plans)!=1:return None,received
    steps=plans[0].get('steps') or []
    if len(motion.get('targets_ticks') or [])!=len(steps) or index>=len(steps):return None,received
    configuration=(analysis.get('configurations') or {}).get(context_id) or {}
    if context.get('episode_id') and (configuration.get('episode') or {}).get('id')!=context['episode_id']:return None,received
    return {**steps[index],'progress':f'{index+1}/{len(steps)}'},received


def display_fields(info):
    completed=timestamp(info['completed_at'])
    if info['completed_at'] is not None:completed+=' · '+info['completion_source']
    duration=info['duration_seconds']
    return [('검사 지정 스텝',info['inspection_step']),('중단 시 진행 스텝',info['interrupted_step']),
            ('마지막 완료 스텝',info['last_completed_step']),('검사 시작',timestamp(info['started_at'])),
            ('판정 완료',completed),('검사 소요',f'{duration:.3f}초' if duration is not None else '미확인')]
