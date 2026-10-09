"""Completion markers are attached to stable step IDs, never list positions."""
EVENT_LABELS={'LOWER':'하단 완료','MIDDLE':'중단 완료','UPPER':'상단 완료','PICK':'집기 완료','PLACE':'적재 완료'}

def legacy_events(steps):
    result={}
    for step in steps:
        name=step['name'];event=next((key for prefix,key in (('하단','LOWER'),('중단','MIDDLE'),('상단','UPPER')) if name.startswith(prefix)),None)
        if name.startswith('완제품'):event='PLACE' if '놓' in name else 'PICK'
        if event:result[event]=step['id']
    return result

def events_for(episode):
    return dict(episode['completion_events']) if 'completion_events' in episode else legacy_events(episode['steps'])

def validate_events(episode):
    if 'completion_events' not in episode:return
    events=episode['completion_events'];steps={s['id']:s for s in episode['steps']}
    if not isinstance(events,dict) or set(events)-EVENT_LABELS.keys():raise ValueError('지원하지 않는 완료 알림입니다.')
    if any(not isinstance(key,str) or key not in steps or steps[key].get('safe_boundary') for key in events.values()):raise ValueError('완료 알림은 저장된 일반 스텝에 지정하세요.')
    if len(set(events.values()))!=len(events):raise ValueError('한 스텝에는 하나의 완료 알림만 지정하세요.')

def set_event(episode,step_id,label):
    events=events_for(episode)
    events={kind:key for kind,key in events.items() if key!=step_id}
    if label!='없음':
        kind=next((kind for kind,value in EVENT_LABELS.items() if value==label),None)
        if kind is None:raise ValueError('완료 알림을 선택하세요.')
        events[kind]=step_id
    episode['completion_events']=events

def event_label(episode,step_id):
    return next((EVENT_LABELS[k] for k,v in events_for(episode).items() if v==step_id),'없음')
