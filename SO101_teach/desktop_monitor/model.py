"""Presentation of recorded execution evidence; never synthesizes a PASS."""
from datetime import datetime
import re
import math
import io
from integration.work_monitor import print_event

LABELS={'ACCEPTED':'작업 접수','FOLLOWER_CONNECTING':'연결 중','FOLLOWER_READY':'연결 확인',
'JIG_DETECTED':'지그 검출','JIG_POSE':'지그 채택','JIG_CONFIRMED':'지그 확정','STEP_DONE':'스텝 완료',
'INSPECTION_STARTED':'검사 시작','INSPECTION_COUNT':'판정 횟수','INSPECTION_PASS':'검사 통과',
'INSPECTION_PROVISIONAL_PASS':'안착 확인 · 위치 확정 대기','INSPECTION_INVALIDATED':'안착 재검사',
'INSPECTION_GROUP_STARTED':'검사 묶음 시작','STARTUP_INSPECTION_STARTED':'시작 조건 검사',
'STARTUP_INSPECTION_PASSED':'시작 조건 통과','LINEAR_CHECK':'리니어 확인','LINEAR_STARTED':'리니어 이동',
'LINEAR_DONE':'리니어 구동 시간 완료','LINEAR_READY':'리니어 준비','PLAN_READY':'경로 준비 완료',
'STAGE_DONE':'단계 완료','WORK_READY':'작업 준비 완료','DONE':'작업 완료','CHAIN_DONE':'연속 작업 완료',
'BUILD_DONE':'조립 완료','EPISODE_DONE':'에피소드 완료','PAUSED':'작업 보류','RESUMED':'작업 재개',
'FAILED':'작업 실패','REJECTED':'작업 거절','INSPECTION_FAILED':'검사 실패',
'ACTION_ACCEPTED':'Action 접수','ACTION_RECEIVED':'명령 수신','ACTION_RESULT':'Action 결과',
'CONTROL_ACCEPTED':'제어 명령 접수','CONTROL_RESULT':'제어 명령 결과',
'RESET_STARTED':'초기화 시작','RESET_DONE':'초기화 완료','SHUTDOWN_DONE':'연결 정리 완료',
'TORQUE_OFF_REQUESTED':'토크 해제 요청','TORQUE_OFF_CONFIRMED':'토크 OFF 확인'}
ARMS={'arm2':'팔2','arm3':'팔3','workcell':'조립대'}
EXPECTED={'housing_seated':'하단 안착','insert_added':'중단 삽입','cap_added':'상단 결합','empty':'부품 없음','present':'부품 있음'}


def parts(row):
    structured=row.get('event') or {}
    if structured.get('type'):return structured['type'],structured.get('detail') or ''
    status=row.get('status','');status=re.sub(r'^(A|B|SLIDE|HOME)_','',status)
    kind,_,detail=status.partition(':');return kind,detail


def human(text,names):
    return re.sub(r'\b[0-9a-f]{32}\b',lambda m:names.get(m[0],m[0][:8]),str(text))


def event_row(row,names):
    kind,detail=parts(row)
    if kind in ('LINEAR_DONE','LINEAR_READY'):detail+=' · 위치 실측 없음'
    tone='error' if any(s in kind for s in ('FAILED','REJECTED','ERROR','INVALIDATED')) or ':ERROR' in row.get('status','') else 'pass' if kind in ('INSPECTION_PASS','DONE','CHAIN_DONE','STEP_DONE','JIG_POSE') else 'pending'
    return (datetime.fromtimestamp(row['at']).strftime('%H:%M:%S'),ARMS.get(row.get('arm'),row.get('arm','—')),
            LABELS.get(kind,kind),human(detail,names)),tone


def record_lines(snapshot,run,*,all_events=True,details=False,category='전체'):
    """Use the terminal formatter, including its failure and timing summaries."""
    rendered=[];failures={}
    for row in run_events(snapshot,run,all_events):
        values,tone=event_row(row,snapshot.get('names',{}))
        if category!='전체' and not (category=='오류' and tone=='error' or category in values[2]):continue
        output=io.StringIO()
        print_event(row,snapshot.get('names',{}),details=details,failures=failures,file=output)
        text=output.getvalue()
        if text:rendered.append((text,tone,row['run_id'],row['sequence']))
    return rendered


def current_run(snapshot,selected=None):
    runs=snapshot.get('runs',[]);control=snapshot.get('control',{});info=control.get('info') or {}
    if selected:return next((r for r in runs if r['run_id']==selected),None)
    if info.get('run_id'):return next((r for r in runs if r['run_id']==info['run_id']),None)
    if control.get('job_id') and info:return next((r for r in runs if r['job_id']==control['job_id']),None)
    return runs[0] if runs else None


def run_events(snapshot,run,all_events=False):
    rows=snapshot.get('events',[])
    if all_events:return rows
    if not run:return []
    related={r['run_id'] for r in snapshot.get('runs',[]) if r['job_id']==run['job_id']}
    execution_ids={r['run_id'] for r in snapshot.get('runs',[])}
    return [r for r in rows if r['run_id'] in related or r.get('job_id')==run['job_id']
            or r['run_id'] not in execution_ids and run['started']<=r['at']<=run['at']+1]


def summary(snapshot,run):
    control=snapshot.get('control',{});info=control.get('info') or {};names=snapshot.get('names',{})
    live=bool(run and info and (info.get('run_id')==run['run_id'] if info.get('run_id') else control.get('job_id')==run['job_id'] and info.get('arm')==run.get('arm')))
    phase='상태 확인 불가' if not control.get('known') else '작업 보류' if live and control.get('paused') else '실행 중' if live else '실행기 응답 있음 · 작업 기록 수신 대기' if info and run is None else '선택한 작업의 이전 기록' if info else '현재 작업 없음 · 최근 기록' if run else '현재 작업 없음'
    if not run:return {'phase':phase,'task':'—','last':'—','current':'—','jigs':[],'checks':[],'live':False}
    rows=run_events(snapshot,run);arm=run.get('arm');jigs={};checks={};last='—';current='종료 기록' if not live else '실행 준비 / 상태 대기'
    if not live:
        end_kind,_=parts({'status':run.get('status','')})
        current='마지막 결과: '+LABELS.get(end_kind,end_kind)
    expected=run.get('expected',{}).get(arm,[])
    for pose_set in run.get('poses',[]):
        if pose_set.get('arm')!=arm:continue
        for pose in pose_set.get('poses',[]):
            key=pose.get('jig_id')
            if key and all(isinstance(pose.get(k),(int,float)) for k in ('x_mm','y_mm','yaw_deg')):
                text=':'.join(f'{k}={pose[k]:.2f}' for k in ('x_mm','y_mm','yaw_deg'))
                jigs[key]={'id':key,'name':names.get(key,key[:8]),'pose':text,'at':None}
    for s in expected:
        check=s.get('check') or {};label=EXPECTED.get(check.get('expected'),str(check.get('expected','')))
        position=next((i+1 for context in run.get('contexts',{}).values() for i,step in enumerate((context.get('episode') or {}).get('steps',[])) if step['id']==s['step_id']),None)
        name=(s.get('step_name') or names.get(s['step_id'],s['step_id'])) if s['step_id'].startswith('start-') else label
        checks[s['step_id']]={'id':s['step_id'],'check':check,'product':s.get('product'),'name':name,'state':'대기','reason':label,'trigger':f'{position}번 완료 후' if position else '시작 전' if s['step_id'].startswith('start-') else s.get('step_name','')}
    for row in rows:
        kind,detail=parts(row)
        if row.get('arm')!=arm:continue
        if kind=='STEP_DONE':last=human(detail,names)
        if kind=='JIG_POSE':
            key,_,pose=detail.partition(':');jigs[key]={'id':key,'name':names.get(key,key[:8]),'pose':pose,'at':row['at']}
        if kind.startswith('INSPECTION_') and kind not in ('INSPECTION_GROUP_STARTED','INSPECTION_GROUP_RESTARTED'):
            key,_,reason=detail.partition(':')
            if kind not in ('INSPECTION_STARTED','INSPECTION_COUNT','INSPECTION_PASS','INSPECTION_PROVISIONAL_PASS','INSPECTION_INVALIDATED'):continue
            item=checks.setdefault(key,{'id':key,'name':names.get(key,key[:8]),'state':'대기','reason':''})
            item['state']={'INSPECTION_STARTED':'검사 중','INSPECTION_COUNT':'검사 중','INSPECTION_PASS':'통과','INSPECTION_PROVISIONAL_PASS':'위치 확정 대기','INSPECTION_INVALIDATED':'재검사'}[kind]
            item['_at']=row['at']
            if kind in ('INSPECTION_STARTED','INSPECTION_INVALIDATED'):item['_started_at']=row['at']
            if kind=='INSPECTION_PASS':item['_result_at']=row['at']
            if reason:item['reason']=human(reason,names)
        if kind in ('FAILED','INSPECTION_FAILED'):
            for item in checks.values():
                if item['state'] in ('검사 중','재검사','위치 확정 대기'):item.update(state='실패',reason=human(detail,names),_at=row['at'],_result_at=row['at'])
    for row in run.get('checks',{}).get(arm,{}).get('records',[]):
        key=row.get('step_id')
        if key and row.get('result') in ('PASS','FAIL'):
            item=checks.setdefault(key,{'id':key,'name':row.get('step_name',names.get(key,key[:8]))})
            if row.get('at',0)>=item.get('_at',0):
                expected=row.get('check',{}).get('expected','')
                item.update(state='통과' if row['result']=='PASS' else '실패',reason=row.get('error') or EXPECTED.get(expected,expected),_at=row.get('at',0),_result_at=row.get('at',0))
    # Existing execution observations identify a target only when correlated to
    # an active recorded motion. Never infer "moving" solely from last STEP_DONE.
    observations=[r for r in run.get('follower',[]) if r.get('arm')==arm]
    if live and observations:
        state=observations[-1]
        if state.get('state')=='MOVING':
            current='이동 중 · 실행기 완료 '+str(state.get('index',0))+'스텝'
            motion=next((m for m in reversed(run.get('motions',[])) if m['arm']==arm and m.get('finished_elapsed_s') is None
                         and len(m.get('requests') or [])==1 and m['requests'][0].get('request_id')==state.get('request_id')),None)
            plan=next((p for p in run.get('plans',[]) if motion and p['plan_id']==motion.get('plan_id')),None)
            index=state.get('index')
            if plan and type(index) is int and 0<=index<len(plan['steps']):
                step=plan['steps'][index];episode=run.get('contexts',{}).get(plan.get('context_id'),{}).get('episode') or {}
                full=episode.get('steps',plan['steps']);position=next((i+1 for i,s in enumerate(full) if s['id']==step['id']),index+1)
                current=f'이동 중 · {position:02}/{len(full)} · '+step['name']
        else:current={'HOLD':'자세 유지 / 검사·다음 명령 대기','READ_ONLY':'토크 OFF · 읽기 상태'}.get(state.get('state'),state.get('state') or current)
    matched=re.match(r'^[A-Z_]+',run.get('status',''))
    command=run.get('operation') or (matched[0] if matched else run.get('status','기록'))
    priority={'실패':0,'재검사':1,'검사 중':2,'위치 확정 대기':3,'대기':4,'통과':5}
    ordered=sorted(checks.values(),key=lambda item:priority.get(item['state'],4))
    return {'phase':phase,'task':ARMS.get(arm,arm or '—')+' · '+command.upper(),'command':command.upper(),'arm':ARMS.get(arm,arm or '—'),
            'last':last,'current':current,'jigs':list(jigs.values()),'checks':ordered,'live':live}


def adopted_outlines(snapshot,run):
    """Draw this run's confirmed coordinates, including after detection pauses."""
    result=summary(snapshot,run)
    if not result['live']:return []
    camera=snapshot.get('camera',{})
    if camera.get('arm')!=run.get('arm'):return []
    invalidated=max((r['at'] for r in run_events(snapshot,run)
                     if parts(r)[0]=='JIG_RECHECK_AFTER_RESUME'),default=0)
    recorded={}
    for group in run.get('poses',[]):
        if group.get('arm')!=run.get('arm') or group.get('adopted_at',0)<invalidated:continue
        for pose in group.get('poses',[]):
            if pose.get('quad'):
                recorded[pose['jig_id']]={'quad':pose.get('outline_px') or pose['quad'],
                    'center_xy_mm':[pose.get('x_mm'),pose.get('y_mm')],'yaw_deg':pose.get('yaw_deg'),
                    'center_px':pose.get('center_px'),'image_size':group.get('image_size')}
    out=[]
    for jig in result['jigs']:
        if invalidated and (jig.get('at') or 0)<invalidated:continue
        observed=recorded.get(jig['id']) or camera.get('frozen',{}).get(jig['id'])
        if not observed:continue
        values=dict(re.findall(r'(x_mm|y_mm|yaw_deg)=(-?\d+(?:\.\d+)?)',jig['pose']))
        try:
            xy=observed['center_xy_mm'];yaw=float(observed['yaw_deg'])
            if not all(math.isfinite(float(v)) for v in (*xy,yaw)):continue
            if max(abs(float(values['x_mm'])-xy[0]),abs(float(values['y_mm'])-xy[1]))>.1:continue
            if abs((float(values['yaw_deg'])-yaw+180)%360-180)>.1:continue
            quad=observed['quad']
            if not 3<=len(quad)<=4096 or not all(len(p)==2 and all(type(v) in (int,float) and math.isfinite(v) and abs(v)<100000 for v in p) for p in quad):continue
        except (TypeError,ValueError,KeyError,IndexError):continue
        out.append({'quad':quad,'name':jig['name'],'id':jig['id'],'yaw_deg':yaw,
                    'center_px':observed.get('center_px'),'image_size':observed.get('image_size')})
    return out
