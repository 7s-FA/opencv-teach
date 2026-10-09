"""Read-only terminal view of the episode CLI; never acquires device control."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
import re

from action_protocol import control_state

ROOT = Path(__file__).resolve().parents[1]


def current_state(root):
    if not (root / 'data/episode-cli.sock').exists():
        return 'IDLE | 진행 중인 CLI/Action 작업 없음'
    try:
        state = control_state(root)
        info = state.get('info')
        if not info:
            return 'STARTING | 작업 준비 중'
        word = 'PAUSED (팔/작업 보류 · 리니어는 계속 진행 가능)' if state.get('paused') else 'RUNNING'
        return f"{word} | {info['arm']}"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f'UNKNOWN | 현재 작업 상태를 확인할 수 없음: {exc}'


class Events:
    def __init__(self, root):
        self.folder = root / 'data/episode-cli-runs'
        self.seen = {}

    def read_new(self):
        events, errors = [], []
        for path in self.folder.glob('*.json'):
            try:
                stamp = path.stat().st_mtime_ns
                old_stamp, count = self.seen.get(path, (None, 0))
                if stamp == old_stamp:
                    continue
                data = json.loads(path.read_text())
                rows = data['events']
                # Validate before advancing the cursor so a partial file can be retried.
                for row in rows:
                    float(row['at'])
                    if not isinstance(row['status'], str) or not isinstance(row['arm'], str):
                        raise ValueError('알림 형식 오류')
                if len(rows) < count:
                    count = 0
                events.extend(rows[count:])
                self.seen[path] = (stamp, len(rows))
            except (OSError, ValueError, KeyError, TypeError) as exc:
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
    status=re.sub(r'\b[0-9a-f]{32}\b',lambda m:names.get(m.group(),'대상'),status)
    for key,name in names.items():
        status=status.replace(':'+key+':',':'+name+':')
        if status.endswith(':'+key) or status.endswith(' · '+key):status=status[:-len(key)]+name
    parts={'housing':'하단','insert':'중단','cap':'상단','pallet':'완성품 팔레트','1':'좌측 팔레트','2':'우측 팔레트'}
    return re.sub(r'start-\d+-([AB])-(housing|insert|cap|pallet|1|2)\b',lambda m:m[1]+' '+parts[m[2]],status)


# Keep unknown events visible; only known routine diagnostics are folded away.
QUIET_EVENTS={
    'ACCEPTED','FOLLOWER_CONNECTING','FOLLOWER_READY','STEP_DONE','INSPECTION_COUNT',
    'INSPECTION_STARTED','LINEAR_CHECK','LINEAR_PROGRESS','JIG_DETECTED','JIG_POSE',
    'SAFE_POSE_STARTED','SAFE_POSE_REACHED','PLAN_READY','WORK_READY','EPISODE_DONE',
    'TORQUE_OFF_STARTED','TORQUE_OFF_CONFIRMED','TORQUE_OFF_REQUESTED',
}
EVENT_LABELS={
    'PLAN_STARTED':'동작 경로 준비', 'EPISODE_STARTED':'에피소드 시작',
    'BUILD_DONE':'조립 완료 · 적재로 전환', 'IDLE_RELEASE_SCHEDULED':'새 명령 대기 · 유휴 시 토크 해제 예약',
    'DONE':'에피소드 완료', 'STARTED':'작업 시작',
    'JIG_DETECTION_STARTED':'지그 위치 확인 시작', 'JIG_CONFIRMED':'지그 위치 확인 완료',
    'JIG_RECHECK_AFTER_RESUME':'재개 전 지그 위치 다시 확인',
    'STARTUP_INSPECTION_STARTED':'시작 조건 검사', 'STARTUP_INSPECTION_PASSED':'시작 조건 검사 통과',
    'INSPECTION_GROUP_STARTED':'검사 시작', 'INSPECTION_GROUP_RESTARTED':'검사 다시 시작',
    'INSPECTION_PASS':'검사 통과', 'STAGE_DONE':'단계 완료',
    'ESTOP_REQUESTED':'팔 정지 요청', 'PAUSED':'팔 정지·작업 보류 확인', 'RESUMED':'작업 재개',
    'LINEAR_CONTINUES':'팔 정지 중에도 리니어는 기존 목표까지 계속 이동',
    'RESET_WAIT_LINEAR_COMPLETE':'안전 복귀 전 리니어 완료 대기',
    'CHAIN_STARTED':'조립·적재 연속 작업 시작', 'CHAIN_LOAD_STARTED':'조립 완료 → 적재 시작',
    'CHAIN_DONE':'조립·적재 전체 완료', 'SAFE_RETURN_STARTED':'안전 자세 복귀 시작',
    'SAFE_RETURN_DONE':'안전 자세 도달', 'SAFE_RETURN_SETTLING':'토크 유지 · 정지 상태 확인 중',
    'SAFE_RETURN_SETTLED':'정지 확인 완료 · 토크 해제 준비', 'RESET_STARTED':'안전 초기화 시작',
}
STAGE_NAMES={'LOWER':'하단','MIDDLE':'중단','UPPER':'상단','PICK':'집기','PLACE':'놓기'}


def routine_event(event):
    status=event['status']
    shutdown={'SHUTDOWN_ACCEPTED':'Pi 안전 종료 접수 · 앱 연결 없이 계속 진행',
              'SHUTDOWN_SETTLING':'두 팔 안전 자세 도달 · 3초 정지 확인 중',
              'SHUTDOWN_DONE':'안전 종료 완료 · 토크 OFF · 새 명령 대기'}
    if status in shutdown:return {**event,'status':shutdown[status]}
    if status.startswith('SHUTDOWN_FAILED:'):return {**event,'status':'Pi 안전 종료 실패 · 상태 확인 필요\n사유: '+status.split(':',1)[1]}
    if status.startswith('SHUTDOWN_ARM_RESET:') or status in ('SHUTDOWN_STARTING','SHUTDOWN_RESET_DONE'):return None
    if status=='IDLE_TORQUE_RELEASE_REQUESTED':return {**event,'status':'새 명령 없음 · 3초 정지 후 토크 해제 요청'}
    if status.startswith(('GOAL_RECEIVED:','CONTROL_ACCEPTED:')):return None
    if status.startswith('CONTROL_RESULT:') and status.endswith(':IDLE'):return None
    if status.startswith('CONTROL_REPLY:') and status.rsplit(':',1)[-1] in ('ESTOP_REQUESTED','RESTART_REQUESTED','RESET_REQUESTED'):return None
    if status=='ACTION_SERVER_READY':return {**event,'status':'명령 서버 준비 완료'}
    if status.startswith('GOAL_ACCEPTED:'):return {**event,'status':'명령 접수: '+status.split(':',1)[1]}
    control=re.fullmatch(r'CONTROL_(estop|restart):REQUESTED',status)
    if control:return {**event,'status':'팔 정지 요청' if control[1]=='estop' else '재개 요청'}
    error=re.match(r'^(GOAL_REJECTED|CONTROL_REJECTED|CONTROL_FAILED|ACTION_FAILED):([^:]+):(.*)',status,re.S)
    if error:
        label={'GOAL_REJECTED':'명령 거절','CONTROL_REJECTED':'제어 거절','CONTROL_FAILED':'제어 실패','ACTION_FAILED':'실행 실패'}[error[1]]
        return {**event,'status':label+': '+error[2]+'\n사유: '+error[3]}
    match=re.match(r'^(A|B|SLIDE)_([A-Z_]+)(?::(.*))?$',status,re.S)
    if not match:return event
    product,kind,value=match.groups();value=value or ''
    if kind in QUIET_EVENTS:return None
    prefix=product+' · ' if product!='SLIDE' else ''
    if kind in ('LINEAR_STARTED','LINEAR_READY'):
        target=re.search(r'target_mm=([0-9.]+)',value)
        if not target:return event
        label='리니어 이동 시작' if kind=='LINEAR_STARTED' else '리니어 목표 구동 완료 상태 확인'
        return {**event,'status':label+' · 목표 '+target[1]+' mm'+(' (위치 실측 없음)' if kind=='LINEAR_READY' else '')}
    if kind=='LINEAR_DONE':return {**event,'status':'리니어 구동 시간 완료 · 위치 실측 없음'}
    if kind not in EVENT_LABELS:return event
    label=EVENT_LABELS[kind]
    if kind=='STAGE_DONE':value=STAGE_NAMES.get(value,value)
    if kind=='INSPECTION_GROUP_STARTED':value=value+'개 항목'
    if kind=='LINEAR_CONTINUES':value=''
    if kind.startswith('CHAIN_'):value=''
    return {**event,'status':prefix+label+(' · '+value if value else '')}


def visible_event(event):
    """Summarize reset progress without modifying the diagnostic history."""
    status=event['status']
    if status in ('GOAL_RECEIVED:RESET','GOAL_RECEIVED:reset','CONTROL_ACCEPTED:RESET','CONTROL_ACCEPTED:reset'):
        return None
    nested=re.match(r'^(?:[AB]|SLIDE)_ARM_RESET:(arm[23]):(.*)',status,re.S)
    if nested:
        if nested[2].startswith(('RESET_WAIT_LINEAR_COMPLETE','LINEAR_CONTINUES','FAILED','REJECTED')):
            return routine_event({**event,'arm':nested[1],'status':'SLIDE_'+nested[2]})
        return None
    if re.fullmatch(r'(?:[AB]|SLIDE)_RESET_DONE',status):
        return {**event,'arm':'workcell','status':'전체 팔 RESET 완료'}
    if status.startswith('CONTROL_REPLY:reset:'):
        detail=status.split(':',2)[2].strip().splitlines()
        if not detail:return None
        last=detail[-1]
        if last.endswith('_RESET_DONE') or last=='RESET_REQUESTED':return None
        return {**event,'status':'RESET 응답: '+last}
    match=re.fullmatch(r'(?:[AB]|SLIDE)_ARM_RESET_(STARTED|DONE):(arm[23])',status)
    if match:
        return {**event,'arm':match[2],'status':'안전 복귀 시작' if match[1]=='STARTED' else '안전 복귀·토크 OFF 완료'}
    if status=='CONTROL_RESET:REQUESTED':return {**event,'status':'전체 팔 RESET 요청'}
    if status in ('CONTROL_RESULT:RESET:IDLE','CONTROL_RESULT:reset:IDLE'):return None
    return routine_event(event)


def print_event(event,names=None,*,details=False,failures=None):
    if not details:
        event=visible_event(event)
        if event is None:return
    when = datetime.fromtimestamp(float(event['at'])).astimezone().strftime('%m-%d %H:%M:%S')
    status=display_status(event['status'],names or {})
    if not details and status.startswith('ACTION_RESULT:'):
        _,command,result=status.split(':',2)
        if result!='IDLE':print(f'[{when}] {event["arm"]} | 실행 결과: {command} 실패 ({result})',flush=True)
        print('─'*72,flush=True);return
    failed=re.match(r'^([AB]|SLIDE)_(INSPECTION_FAILED|FAILED|REJECTED):(.*)',status,re.S)
    if failed and not details:
        product,kind,reason=failed.groups()
        key=(event.get('run_id',event['arm']),product,reason)
        previous=(failures or {}).get(key)
        repeated=previous is not None and 0 <= float(event['at'])-previous < 15
        title={'INSPECTION_FAILED':'검사 실패','FAILED':'작업 실패','REJECTED':'작업 거절'}[kind]
        print(f"[{when}] {event['arm']} | {product}_{kind}: {product} {title}",flush=True)
        if repeated:
            print('  상세: 위에 표시한 동일 실패 원인 참조',flush=True)
        else:
            print('  상세:',flush=True)
            context,separator,body=reason.partition(':')
            if separator:print('    '+context.strip(),flush=True)
            for line in re.split(r'\s+·\s+|\n',body if separator else reason):
                if line.strip():print('    - '+line.strip(),flush=True)
        if failures is not None:
            failures[key]=float(event['at'])
            for old in list(failures):
                if float(event['at'])-failures[old]>=15:del failures[old]
    else:
        lines=status.splitlines() or ['']
        print(f"[{when}] {event['arm']} | {lines[0]}",flush=True)
        for line in lines[1:]:print('  '+line,flush=True)

    if status.startswith('ACTION_RESULT:'):
        print('─'*72,flush=True)


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
        if previous is None or previous.get(service)!=state:print(f'서비스 {service}: {state}', flush=True)
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
