"""Read-only terminal view of the episode CLI; never acquires device control."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
import re
import sys

if __package__:
    from .action_protocol import control_state
    from . import episode_timing
else:
    from action_protocol import control_state
    import episode_timing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from so101_teach.execution_records import RunReader
from so101_teach.run_directory import RunDirectoryWatch


def arm_name(value):return {'arm2':'팔2','arm3':'팔3','workcell':'조립대'}.get(value,value)


def current_state(root):
    if not (root / 'data/episode-cli.sock').exists():
        return '대기 | 진행 중인 작업 없음'
    try:
        state = control_state(root)
        info = state.get('info')
        if not info:
            return '준비 중 | 작업 준비 중'
        word = '보류 (팔/작업 보류 · 리니어는 계속 진행 가능)' if state.get('paused') else '실행 중'
        return f"{word} | {arm_name(info['arm'])}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f'확인 불가 | 현재 작업 상태를 확인할 수 없음: {exc}'


class Events:
    def __init__(self, root):
        self.folder = root / 'data/episode-cli-runs'
        self.seen = {}
        self.reader=RunReader();self.changes=RunDirectoryWatch(self.folder);self.retry=set()

    def read_new(self):
        events, errors = [], []
        changed,rescan=self.changes.poll()
        paths=set(self.folder.glob('*.json')) if rescan else changed
        retry=self.retry;paths|=retry;self.retry=set()
        for path in paths:
            try:
                stamp = path.stat().st_mtime_ns
                old_stamp, count = self.seen.get(path, (None, 0))
                if stamp == old_stamp and path not in changed and path not in retry:
                    continue
                data = self.reader.read(path,copy=False)
                rows = data['events']
                # Validate before advancing the cursor so a partial file can be retried.
                # The journal reader validates every committed delta. Legacy
                # snapshots retain full validation, including rewritten history.
                for row in rows:
                    float(row['at'])
                    if not isinstance(row['status'], str) or not isinstance(row['arm'], str):
                        raise ValueError('알림 형식 오류')
                if len(rows) < count:
                    count = 0
                events.extend(rows[count:])
                self.seen[path] = (stamp, len(rows))
            except FileNotFoundError:self.seen.pop(path,None)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.retry.add(path)
                errors.append(f'{path.name}: {exc}')
        return sorted(events, key=lambda row: float(row['at'])), errors


def display_names(root):
    names={}
    root=Path(root)
    try:recipes=json.loads((root/'integration/recipes.json').read_text()).get('recipes',{})
    except (OSError,ValueError,AttributeError):recipes={}
    for arm,folder in (('arm2',root/'data'),('arm3',root/'data/arm3-runtime')):
        try:
            for key,value in json.loads((folder/'jigs.json').read_text()).items():names[key]=value.get('name') or '지그'
        except (OSError,ValueError,TypeError,AttributeError):pass
        for recipe in recipes.get(arm,{}).values():
            key=recipe.get('episode_id','')
            if not re.fullmatch('[0-9a-f]{32}',key):continue
            try:
                for step in json.loads((folder/'episodes'/(key+'.json')).read_text()).get('steps',[]):names[step['id']]=step.get('name') or '스텝'
            except (OSError,ValueError,TypeError,KeyError,AttributeError):pass
    return names


def display_status(status,names):
    # IDs remain in the source history. Only terminal presentation is shortened.
    if status.startswith('실패 영상 저장: '):return status
    status=re.sub(r'\b[0-9a-f]{32}\b',lambda m:names.get(m.group(),'대상'),status)
    for key,name in names.items():
        status=status.replace(':'+key+':',':'+name+':')
        if status.endswith(':'+key) or status.endswith(' · '+key):status=status[:-len(key)]+name
    parts={'housing':'하단','insert':'중단','cap':'상단','pallet':'완성품 팔레트','1':'좌측 팔레트','2':'우측 팔레트'}
    return re.sub(r'start-\d+-([AB])-(housing|insert|cap|pallet|1|2)\b',lambda m:m[1]+' '+parts[m[2]],status)


# Keep unknown events visible; only known routine diagnostics are folded away.
QUIET_EVENTS={
    'CHAIN_SAFE_DETAIL',
    'ACCEPTED','FOLLOWER_CONNECTING','FOLLOWER_READY','STEP_DONE','INSPECTION_COUNT',
    'INSPECTION_STARTED','LINEAR_CHECK','LINEAR_PROGRESS','JIG_DETECTED','JIG_POSE',
    'SAFE_POSE_STARTED','SAFE_POSE_REACHED','PLAN_READY','WORK_READY','EPISODE_DONE',
    'TORQUE_OFF_STARTED','TORQUE_OFF_CONFIRMED','TORQUE_OFF_REQUESTED',
}
EVENT_LABELS={
    'PLAN_DISCARDED':'준비 조건 변경 · 기존 경로 폐기',
    'RESET_TORQUE_ON_STARTED':'안전 복귀를 위한 토크 켜기 시작',
    'RESET_TORQUE_ON_CONFIRMED':'안전 복귀를 위한 토크 켜짐 확인',
    'INSPECTION_MODEL_PREPARING':'안착 검사 모델 준비 중',
    'PREPARATION_STARTED':'실행 준비 시작', 'LOAD_MODEL_PREPARING':'하차 검사 모델 미리 준비 중',
    'CHAIN_SAFE_STARTED':'두 팔 시작 안전자세 동시 이동 준비',
    'CHAIN_SAFE_ARM_STARTED':'시작 안전자세 이동 시작', 'CHAIN_SAFE_ARM_DONE':'시작 안전자세 도착',
    'CHAIN_SAFE_ARM_SKIPPED':'시작 안전자세 일치 · 이동 생략',
    'SAFE_POSE_SKIPPED':'시작 안전자세 일치 · 이동 생략',
    'CHAIN_SAFE_DONE':'두 팔 시작 안전자세 준비 완료',
    'WORKCELL_PREPARING':'양팔·카메라·모델 준비 중','WORKCELL_READY':'양팔·카메라·모델 준비 완료',
    'STANDBY_READY':'3분 준비 대기 · 다음 명령 수신 가능',
    'VISION_PREPARING':'카메라·모델 준비 중', 'VISION_READY':'카메라·모델 준비 완료',
    'PLAN_STARTED':'동작 경로 준비', 'EPISODE_STARTED':'에피소드 시작',
    'BUILD_DONE':'조립 완료 · 적재로 전환', 'IDLE_RELEASE_SCHEDULED':'새 명령 대기 · 유휴 시 토크 해제 예약',
    'DONE':'에피소드 완료', 'STARTED':'작업 시작',
    'JIG_DETECTION_STARTED':'지그 위치 확인 시작', 'JIG_CONFIRMED':'지그 위치 확인 완료',
    'JIG_RECHECK_AFTER_RESUME':'재개 전 지그 위치 다시 확인', # Historical records.
    'PREPARATION_RECHECK_AFTER_RESUME':'기존 지그 위치 유지 · 실행 준비 재확인',
    'JIG_BASIS_REUSED':'기존에 채택한 지그 위치 유지',
    'STARTUP_INSPECTION_STARTED':'시작 조건 검사', 'STARTUP_INSPECTION_PASSED':'시작 조건 검사 통과',
    'INSPECTION_GROUP_STARTED':'검사 시작', 'INSPECTION_GROUP_RESTARTED':'검사 다시 시작',
    'INSPECTION_PASS':'검사 통과', 'STAGE_DONE':'단계 완료',
    'INSPECTION_PROVISIONAL_PASS':'안착 확인 · 최종 지그 위치 확인 대기',
    'INSPECTION_INVALIDATED':'지그 위치 변경 · 안착 재검사',
    'ESTOP_REQUESTED':'팔 정지 요청', 'PAUSED':'팔 정지·작업 보류 확인', 'RESUMED':'작업 재개',
    'LINEAR_CONTINUES':'팔 정지 중에도 리니어는 기존 목표까지 계속 이동',
    'RESET_WAIT_LINEAR_COMPLETE':'안전 복귀 전 리니어 완료 대기',
    'CHAIN_STARTED':'조립·적재 연속 작업 시작', 'CHAIN_LOAD_STARTED':'조립 완료 → 적재 시작',
    'CHAIN_RETURN_STARTED':'완제품 안착 확인 · 리니어 조립 위치 복귀 시작',
    'CHAIN_RETURN_WAIT':'리니어 조립 위치 복귀 완료 대기',
    'CHAIN_RETURN_DONE':'리니어 조립 위치 복귀 완료 · 구동 시간 기준',
    'CHAIN_DONE':'조립·적재 전체 완료', 'SAFE_RETURN_STARTED':'안전 자세 복귀 시작',
    'SAFE_RETURN_DONE':'안전 자세 도달', 'SAFE_RETURN_SETTLING':'토크 유지 · 정지 상태 확인 중',
    'SAFE_RETURN_SETTLED':'정지 확인 완료 · 토크 해제 준비', 'RESET_STARTED':'안전 초기화 시작',
}
STAGE_NAMES={'LOWER':'하단','MIDDLE':'중단','UPPER':'상단','PICK':'집기','PLACE':'놓기'}
COMMAND_NAMES={'PC_BUILD_LOAD_A':'PC 협력 A 조립·적재','PC_BUILD_LOAD_B':'PC 협력 B 조립·적재','BUILD_LOAD_A':'A 조립·적재','BUILD_LOAD_B':'B 조립·적재',
               'BUILD_SLIDE':'양팔 준비·리니어 조립 위치 이동','LOAD_SLIDE':'리니어 하차 위치 이동',
               'PREPARE_HOME':'두 팔 안전 복귀·리니어 하차 위치 이동','PROTOCOL':'통신 규약',
               'BUILD_A':'A 조립','BUILD_B':'B 조립','LOAD_A':'A 적재','LOAD_B':'B 적재',
               'SLIDE_BUILD':'양팔 준비·리니어 조립 위치 이동','SLIDE_LOAD':'리니어 하차 위치 이동',
               'RESET':'안전 초기화','RESTART':'재개','ESTOP':'팔 정지','EMER_STOP':'팔 정지','CLEAR_ERROR':'오류 해제'}
def command_name(value):return COMMAND_NAMES.get(value.upper(),value)

RESULT_NAMES={'IDLE':'완료','ERROR':'실패','UNKNOWN':'결과 미확인','PENDING':'처리 결과 대기',
              'STORAGE_PENDING':'결과 기록 저장 대기','NOT_FOUND':'미접수 확인','CLEARED':'미확정 기록 정리됨'}
REASON_NAMES={**RESULT_NAMES,'BUSY':'다른 작업 실행 중','CONTROL_PENDING':'이전 제어 요청 처리 중',
              'REQUEST_ID_CONFLICT':'요청 ID와 저장된 명령 불일치',
              'COMM_LOST_OR_EXPIRED_REQUEST':'관제 통신 단절 또는 요청 유효시간 만료',
              'SUPERVISION_V2_REQUIRED':'관제 통신 규약 V2 필요','DISPATCH_UNAVAILABLE':'명령 실행 준비 불가',
              'STALE_PC_STOP_CONTEXT':'현재 정지 상태보다 오래된 제어 요청',
              'UNMONITORED_OR_DISCONNECTED_JOB':'관제 감시가 없거나 연결이 끊긴 작업',
              'NO_ACTIVE_JOB':'진행 중인 작업 없음','STALE_CONTROL':'새 정지 이후 이전 제어 요청 거절',
              'PREPARATION_CHANGED':'준비 조건 변경','RESET':'안전 초기화'}
def result_name(value):return RESULT_NAMES.get(value,value)
def reason_name(value):
    # Translate a known leading code, retaining the actual error and measurements.
    match=re.match(r'^([A-Z_]+)(?=$|[:\s])(.*)',value,re.S)
    return REASON_NAMES.get(match[1],match[1])+match[2] if match else value


def routine_event(event):
    status=event['status']
    cleared=re.fullmatch(r'ABANDONED_REQUESTS_CLEARED:(\d+)',status)
    if cleared:return {**event,'status':'미확정 요청 기록 정리: '+cleared[1]+'건'}
    if status.startswith('COMM_LOST_HOLD_REQUESTED:'):
        return {**event,'status':'관제 통신 끊김 · 팔 정지·작업 보류 요청'}
    if status.startswith('REQUEST_LEDGER_FAILED:'):
        return {**event,'status':'요청 결과 기록 저장 실패: '+status.split(':',1)[1]}
    replay=re.fullmatch(r'REQUEST_REPLAY:([^:]+):([^:]+)',status)
    if replay:return {**event,'status':'기존 요청 결과 조회: '+result_name(replay[2])}
    control_result=re.fullmatch(r'CONTROL_RESULT:([^:]+):(.+)',status,re.S)
    if control_result:
        return {**event,'status':'제어 결과: '+command_name(control_result[1])+' · '+result_name(control_result[2])}
    local_control=re.fullmatch(r'CONTROL_(ESTOP|RESTART|RESET):(REQUESTED|REJECTED)(?::(.*))?',status,re.S)
    if local_control:
        return {**event,'status':command_name(local_control[1])+(' 요청' if local_control[2]=='REQUESTED' else ' 요청 거절: '+reason_name(local_control[3] or ''))}
    output=re.fullmatch(r'EXECUTOR_OUTPUT:([^:]+):(.*)',status,re.S)
    if output:return {**event,'status':'실행부 알림: '+command_name(output[1])+' · '+output[2]}
    shutdown={'SHUTDOWN_ACCEPTED':'Pi 안전 종료 접수 · 앱 연결 없이 계속 진행',
              'SHUTDOWN_SETTLING':'두 팔 안전 자세 도달 · 3초 정지 확인 중',
              'SHUTDOWN_DONE':'안전 종료 완료 · 토크 OFF · 새 명령 대기'}
    if status in shutdown:return {**event,'status':shutdown[status]}
    if status.startswith('SHUTDOWN_FAILED:'):return {**event,'status':'Pi 안전 종료 실패 · 상태 확인 필요\n사유: '+status.split(':',1)[1]}
    if status.startswith('SHUTDOWN_ARM_RESET:') or status in ('SHUTDOWN_STARTING','SHUTDOWN_RESET_DONE'):return None
    if status=='IDLE_TORQUE_RELEASE_REQUESTED':return {**event,'status':'새 명령 없음 · 3초 정지 후 토크 해제 요청'}
    if status.startswith(('GOAL_RECEIVED:','CONTROL_ACCEPTED:')):return None
    if status.startswith('CONTROL_REPLY:') and status.rsplit(':',1)[-1] in ('ESTOP_REQUESTED','RESTART_REQUESTED','RESET_REQUESTED'):return None
    if status=='ACTION_SERVER_READY':return {**event,'status':'명령 서버 준비 완료'}
    if status.startswith('GOAL_ACCEPTED:'):return {**event,'status':'명령 접수: '+command_name(status.split(':',1)[1])}
    request=re.fullmatch(r'CONTROL_REQUEST:(estop|restart|reset):(.+)',status)
    if request:return {**event,'status':command_name(request[1])+' 요청'}
    control=re.fullmatch(r'CONTROL_(estop|restart):REQUESTED',status)
    if control:return {**event,'status':'팔 정지 요청' if control[1]=='estop' else '재개 요청'}
    error=re.match(r'^(GOAL_REJECTED|CONTROL_REJECTED|CONTROL_FAILED|ACTION_FAILED):([^:]+):(.*)',status,re.S)
    if error:
        label={'GOAL_REJECTED':'명령 거절','CONTROL_REJECTED':'제어 거절','CONTROL_FAILED':'제어 실패','ACTION_FAILED':'실행 실패'}[error[1]]
        return {**event,'status':label+': '+command_name(error[2])+'\n사유: '+reason_name(error[3])}
    match=re.match(r'^(A|B|SLIDE|HOME)_([A-Z_]+)(?::(.*))?$',status,re.S)
    if not match:return event
    product,kind,value=match.groups();value=value or ''
    if kind=='RECOVERY_CONTEXT' or kind in QUIET_EVENTS:return None
    prefix=('안전 준비 · ' if product=='HOME' else product+' · ') if product!='SLIDE' else ''
    if kind=='FAILURE_IMAGES':return {**event,'status':'실패 영상 저장: '+value}
    if kind=='FAILURE_IMAGE_SAVE_FAILED':return {**event,'status':'실패 영상 저장 실패: '+value}
    if kind=='PREPARATION_PROFILE':
        try:lines=episode_timing.preparation_lines(json.loads(value),event['arm'])
        except (ValueError,TypeError,KeyError):return event
        return {**event,'status':prefix+'준비 상세 계측\n'+'\n'.join(lines)}
    if kind in ('LINEAR_STARTED','LINEAR_READY','LINEAR_DONE'):
        target=re.search(r'target_mm=([0-9.]+)',value)
        if not target:return {**event,'status':'리니어 구동 시간 완료 · 위치 실측 없음'} if kind=='LINEAR_DONE' else event
        label='리니어 이동 시작' if kind=='LINEAR_STARTED' else '리니어 구동 시간 완료' if kind=='LINEAR_DONE' else '리니어 목표 구동 완료 상태 확인'
        position='조립 위치' if float(target[1])==100. else '하차 위치' if float(target[1])==1.5 else '사용자 지정 위치'
        return {**event,'status':label+' · '+position+' ('+target[1]+' mm)'+(' · 위치 실측 없음' if kind!='LINEAR_STARTED' else '')}
    if kind=='LINEAR_DONE':return {**event,'status':'리니어 구동 시간 완료 · 위치 실측 없음'}
    if kind not in EVENT_LABELS:return event
    label=EVENT_LABELS[kind]
    if kind=='PLAN_DISCARDED':value=''
    if kind=='PREPARATION_STARTED':value=command_name(value)
    if kind=='IDLE_RELEASE_SCHEDULED':
        delay=re.fullmatch(r'seconds=([0-9.]+)',value)
        if delay:value=delay[1]+'초 뒤'
    if kind=='STAGE_DONE':value=STAGE_NAMES.get(value,value)
    if kind=='INSPECTION_GROUP_STARTED':value=value+'개 항목'
    if kind=='LINEAR_CONTINUES':value=''
    if kind=='SAFE_POSE_SKIPPED':value=''
    if kind in ('CHAIN_SAFE_ARM_STARTED','CHAIN_SAFE_ARM_DONE','CHAIN_SAFE_ARM_SKIPPED'):value={'arm2':'로봇팔2','arm3':'로봇팔3'}.get(value,value)
    if kind.startswith('CHAIN_') and kind not in ('CHAIN_SAFE_ARM_STARTED','CHAIN_SAFE_ARM_DONE','CHAIN_SAFE_ARM_SKIPPED'):value=''
    return {**event,'status':prefix+label+(' · '+value if value else '')}


def visible_event(event):
    """Summarize reset progress without modifying the diagnostic history."""
    status=event['status']
    if status in ('GOAL_RECEIVED:RESET','GOAL_RECEIVED:reset','CONTROL_ACCEPTED:RESET','CONTROL_ACCEPTED:reset'):
        return None
    nested=re.match(r'^(?:[AB]|SLIDE|HOME)_ARM_RESET:(arm[23]):(.*)',status,re.S)
    if nested:
        if nested[2].startswith(('RESET_WAIT_LINEAR_COMPLETE','LINEAR_CONTINUES','FAILED','REJECTED')):
            return routine_event({**event,'arm':nested[1],'status':'SLIDE_'+nested[2]})
        return None
    if status=='HOME_RESET_DONE':return {**event,'arm':'workcell','status':'두 팔 안전 복귀·리니어 하차 위치 이동 완료'}
    if re.fullmatch(r'(?:[AB]|SLIDE|HOME)_RESET_DONE',status):
        return {**event,'arm':'workcell','status':'전체 팔 안전 초기화 완료'}
    if status.startswith('CONTROL_REPLY:reset:'):
        detail=status.split(':',2)[2].strip().splitlines()
        if not detail:return None
        last=detail[-1]
        if last.endswith('_RESET_DONE') or last=='RESET_REQUESTED':return None
        return {**event,'status':'안전 초기화 응답: '+last}
    skipped=re.fullmatch(r'(?:[AB]|SLIDE|HOME)_ARM_SAFE_ALREADY:(arm[23])',status)
    if skipped:return {**event,'arm':skipped[1],'status':'안전 자세 일치 · 이동 생략'}
    match=re.fullmatch(r'(?:[AB]|SLIDE|HOME)_ARM_RESET_(STARTED|DONE):(arm[23])',status)
    if match:
        return {**event,'arm':match[2],'status':'안전 복귀 시작' if match[1]=='STARTED' else '안전 복귀 완료 · 토크 유지' if status.startswith('HOME_') else '안전 복귀·토크 해제 완료'}
    if status=='CONTROL_RESET:REQUESTED':return {**event,'status':'전체 팔 안전 초기화 요청'}
    if status in ('CONTROL_RESULT:RESET:IDLE','CONTROL_RESULT:reset:IDLE'):return None
    return routine_event(event)


def print_event(event,names=None,*,details=False,failures=None,file=None):
    if not details:
        event=visible_event(event)
        if event is None:return
    who=event['arm'] if details else arm_name(event['arm'])
    stamp=datetime.fromtimestamp(float(event['at'])).astimezone()
    when=stamp.isoformat(timespec='milliseconds') if details else stamp.strftime('%H:%M:%S')
    if details and 'elapsed_s' in event:when+=f" +{event['elapsed_s']:.3f}s"
    status=event['status'] if details else display_status(event['status'],names or {})
    if not details and status.startswith('ACTION_RESULT:'):
        _,command,result=status.split(':',2)
        if result!='IDLE':
            print(f'[{when}] {who} | 실행 결과: {command_name(command)} '+result_name(result),flush=True,file=file)
        print('─'*72,flush=True,file=file);return
    failed=re.match(r'^([AB]|SLIDE|HOME)_(INSPECTION_FAILED|FAILED|REJECTED):(.*)',status,re.S)
    if failed and not details:
        product,kind,reason=failed.groups();reason=reason_name(reason)
        product={'HOME':'안전 준비','SLIDE':'리니어'}.get(product,product)
        key=(event.get('run_id',event['arm']),product,reason)
        previous=(failures or {}).get(key)
        repeated=previous is not None and 0 <= float(event['at'])-previous < 15
        title={'RECOVERY_REQUIRED':'판정 실패 · 재검사 후 재개 대기',
        'RECOVERY_RECHECK_STARTED':'재개 전 상태·영상 재검사 중',
        'RECOVERY_RECHECK_PASSED':'재검사 통과 · 이어서 실행',
        'RECOVERY_RECHECK_CANCELLED':'새 정지 요청 · 재검사 취소',
        'RECOVERY_BLOCKED':'재개 조건 미충족 · 보류 유지',
        'INSPECTION_RETRY_REQUIRED':'검사 재시도 필요',
        'INSPECTION_RECHECK_STARTED':'체크포인트 새 영상 검사 중',
        'INSPECTION_FAILED':'검사 실패','FAILED':'작업 실패','REJECTED':'작업 거절'}[kind]
        print(f"[{when}] {who} | {product} · {title}",flush=True,file=file)
        if repeated:
            print('  상세: 위에 표시한 동일 실패 원인 참조',flush=True,file=file)
        else:
            print('  상세:',flush=True,file=file)
            context,separator,body=reason.partition(':')
            if separator:print('    '+context.strip(),flush=True,file=file)
            for line in re.split(r'\s+·\s+|\n',body if separator else reason):
                if line.strip():print('    - '+line.strip(),flush=True,file=file)
        if failures is not None:
            failures[key]=float(event['at'])
            for old in list(failures):
                if float(event['at'])-failures[old]>=15:del failures[old]
    else:
        lines=status.splitlines() or ['']
        print(f"[{when}] {who} | {lines[0]}",flush=True,file=file)
        for line in lines[1:]:print('  '+line,flush=True,file=file)

    if event.get('timing_summary'):
        if details:
            for line in episode_timing.summary_lines(event['timing_summary']):print('  '+line,flush=True,file=file)
        else:
            summary=event['timing_summary']
            phases=[f"{episode_timing.LABELS.get(p['phase'],p['phase'])} {p['seconds']:.1f}초"+
                    ('' if p['completed'] else ' (중단)') for p in summary.get('phases',[])]
            if phases:print('  구간: '+' / '.join(phases),flush=True,file=file)
            print(f"  소요 {summary['total_seconds']:.1f}초",flush=True,file=file)
    if status.startswith('ACTION_RESULT:'):
        print('─'*72,flush=True,file=file)


def service_status(previous=None):
    states={}
    for service in ('workcell-actions', 'arm3-linear-controller'):
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', service], capture_output=True, text=True, timeout=3,
            )
            state = result.stdout.strip() or 'UNKNOWN'
        except (OSError, subprocess.TimeoutExpired):
            state = 'UNKNOWN'
        states[service]=state
        names={'workcell-actions':'조립대 명령 서버','arm3-linear-controller':'리니어 제어기'}
        labels={'active':'실행 중','inactive':'중지','failed':'실패','activating':'기동 중','deactivating':'종료 중','UNKNOWN':'확인 불가'}
        if previous is None or previous.get(service)!=state:print(f'서비스 {names[service]}: {labels.get(state,state)}', flush=True)
    return states


def main():
    parser = argparse.ArgumentParser(description='현재 작업과 상세 알림 조회 (장치 제어 없음)')
    parser.add_argument('--details',action='store_true',help='세부 진행·측정값·원문 응답을 모두 표시')
    parser.add_argument('--once', action='store_true', help='현재 상태와 최근 알림을 한 번만 출력')
    parser.add_argument('--tail', type=int, default=15, help='처음에 표시할 최근 알림 수 (기본 15)')
    parser.add_argument('--app-dir', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.tail < 0:
        parser.error('--tail은 0 이상이어야 합니다.')
    root = args.app_dir.expanduser().resolve()
    if not (root / 'integration/episode_cli.py').is_file():
        parser.error('실행부 폴더를 찾지 못했습니다. --app-dir 경로를 확인하세요.')
    print('조립대 작업 조회 (앱 에피소드·CLI·Action / 개별 관절 수동 조작 제외)', flush=True)
    services=service_status();service_checked=time.monotonic()
    previous_state = current_state(root)
    print('현재: ' + previous_state, flush=True)
    reader = Events(root)
    events, errors = reader.read_new()
    print('최근 알림 (이력이며 현재 상태와 구분):', flush=True)
    names=display_names(root);failures={}
    if not args.details:events=[event for event in events if visible_event(event) is not None]
    for event in events[-args.tail:] if args.tail else []:
        print_event(event,names,details=args.details,failures=failures)
    if not events:
        print('기록 없음', flush=True)
    for error in errors:
        print('기록 읽기 오류: ' + error, flush=True)
    if args.once:
        return
    print('새 알림 대기 중 · Ctrl+C: 조회만 종료 (작업은 계속됨)', flush=True)
    previous_errors = set(errors)
    try:
        while True:
            time.sleep(1)
            if time.monotonic()-service_checked>=5:
                services=service_status(services);service_checked=time.monotonic()
            state = current_state(root)
            events, errors = reader.read_new()
            if events:names=display_names(root)
            for event in events:
                print_event(event,names,details=args.details,failures=failures)
            for error in set(errors) - previous_errors:
                print('기록 읽기 오류: ' + error, flush=True)
            previous_errors = set(errors)
            if state != previous_state:
                print('현재: ' + state, flush=True)
                previous_state = state
    except KeyboardInterrupt:
        print('\n조회 종료. 로봇 작업 상태는 변경하지 않았습니다.', flush=True)


if __name__ == '__main__':
    main()
