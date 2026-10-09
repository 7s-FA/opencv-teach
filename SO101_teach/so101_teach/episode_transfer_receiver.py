"""Pi-side episode delivery transaction, executable over SSH without installation.

Only episode snapshots, backup records and one recipes.json slot are written.
There are no controller, service, camera or motion commands in this module.
"""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sys
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def checked_arm(arm):
    if arm not in ('arm2', 'arm3'):
        raise ValueError('지원하지 않는 로봇팔입니다.')
    return arm


def checked_id(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('에피소드 또는 전송 ID가 올바르지 않습니다.')
    return value


def target_state(root, arm):
    from so101_teach.domain import read_json, load_profile
    from so101_teach.arm_workspace import require_calibrated
    root = Path(root)
    data = root / ('data' if checked_arm(arm) == 'arm2' else 'data/arm3-runtime')
    profile, calibration, _ = load_profile(data)
    require_calibrated(profile)
    if profile.get('robot_id', 'arm2') != arm:
        raise ValueError('Pi 저장 폴더와 로봇팔 설정이 다릅니다.')
    recipes = read_json(root / 'integration/recipes.json')
    if recipes.get('schema') != 1 or not isinstance(recipes.get('recipes', {}).get(arm), dict):
        raise ValueError('Pi의 A/B 실행 연결 형식을 확인하세요.')
    settings = read_json(root / 'integration/execution-settings.json')[arm]
    paths = [data / 'profile.json', data / 'jigs.json', data / profile['calibration_file'],
             (data / profile['calibration_file']).with_suffix('.angles.json')]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}
    revision = digest({'recipes': recipes, 'settings': settings, 'files': hashes})
    return data, profile, calibration, recipes, settings, revision


def inspect_target(root, arm):
    data, profile, cal, recipes, settings, revision = target_state(root, arm)
    from so101_teach.domain import EpisodeStore,read_json
    episodes={doc['id']:{'name':doc['name'],'sha256':digest(read_json(path)),'step_count':len(doc['steps'])} for path,doc in EpisodeStore(data/'episodes',cal,arm).entries()}
    return {'arm': arm, 'revision': revision, 'calibration_sha256': cal.sha256,
            'recipes': deepcopy(recipes['recipes'][arm]), 'settings': settings,
            'episodes':episodes,'data_directory': str(data)}


def pull_target(root, arm):
    """Read Pi's episode library without acquiring devices or writing files."""
    from so101_teach.domain import EpisodeStore
    data, profile, cal, recipes, settings, revision = target_state(root, arm)
    store = EpisodeStore(data / 'episodes', cal, arm)
    episodes = [doc for _, doc in store.entries()]
    ids = {doc['id'] for doc in episodes}
    missing = [slot for slot, recipe in recipes['recipes'][arm].items() if recipe['episode_id'] not in ids]
    if missing:
        raise ValueError('Pi 실행 에피소드 검증 실패: ' + ', '.join(missing))
    return {'arm': arm, 'revision': revision, 'calibration_sha256': cal.sha256,
            'recipes': deepcopy(recipes['recipes'][arm]), 'settings': settings,
            'episodes': episodes, 'data_directory': str(data),
            'completion_events_supported': 'completion_events' in (Path(root) / 'integration/episode_cli.py').read_text(),
            'execution_policy': {'linear_target_mm': 100. if arm == 'arm2' else 1.5,
                'prepare_before_safe_pose': True, 'finish_hold_seconds': 5., 'finish_torque_off': True}}


def validated_episode(root, request, *, preserve_id=False):
    from so101_teach.domain import EpisodeStore
    from so101_teach.configuration import JigCatalog
    from so101_teach.jig_compatibility import execution_reference
    arm = checked_arm(request['arm'])
    if not preserve_id and request.get('slot') not in ('A', 'B'):
        raise ValueError('A 또는 B 실행을 선택하세요.')
    checked_id(request.get('transfer_id'))
    timeout = request.get('timeout_seconds')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 86400:
        raise ValueError('실행 제한시간은 1~86400초입니다.')
    data, profile, cal, recipes, settings, revision = target_state(root, arm)
    episode = deepcopy(request['episode'])
    EpisodeStore(data / 'episodes', cal, arm).validate(episode)
    if 'completion_events' in episode:
        events=episode['completion_events'];steps={s['id']:s for s in episode['steps']}
        if (not isinstance(events,dict) or set(events)-{'LOWER','MIDDLE','UPPER','PICK','PLACE'}
                or any(not isinstance(key,str) or key not in steps or steps[key].get('safe_boundary') for key in events.values())
                or len(set(events.values()))!=len(events)):
            raise ValueError('에피소드 완료 알림 지정 오류')
        if 'completion_events' not in (Path(root)/'integration/episode_cli.py').read_text():
            raise ValueError('Pi 실행기가 스텝별 완료 알림을 지원하지 않습니다. 실행기 업데이트 후 전송하세요.')
    if not episode['steps']:
        raise ValueError('저장된 스텝이 없습니다. 스텝을 추가한 뒤 전송하세요.')
    catalog = JigCatalog(data, profile=profile)
    for step in episode['steps']:
        key = step.get('jig_id')
        if not key:
            continue
        if key not in catalog.items:
            raise ValueError('Pi에 등록되지 않은 지그입니다: ' + key)
        mesh = catalog.mesh(key)
        current = {'stl_sha256': mesh['sha256'], 'symmetry_deg': 360,
                   'mesh_yaw_offset_deg': 180.} if mesh.get('assembly', {}).get('orientation_hole') else {'stl_sha256': mesh['sha256']}
        reference = execution_reference(step['jig_reference'], current, mesh['sha256'])
        if reference['stl_sha256'] != mesh['sha256']:
            raise ValueError('Pi 지그 모델과 티칭 기준이 다릅니다: ' + catalog.items[key]['name'])
    if 'product_type' in episode or 'startup_inspections' in episode or any('inspection' in step for step in episode['steps']):
        try:
            from so101_teach.episode_inspection import validate_inspection,has_inspections,inspection_schedule
        except ImportError:raise ValueError('Pi 안착 검사 실행기 업데이트가 필요합니다.') from None
        validate_inspection(episode)
        inspection_schedule(episode['steps'])
        if has_inspections(episode):
            if 'INSPECTION_PROTOCOL = 6' not in (Path(root)/'integration/episode_cli.py').read_text():raise ValueError('Pi 실행기가 B 조립 이력 기반 안착 검사를 지원하지 않습니다. 업데이트 후 전송하세요.')
            from so101_teach.episode_inspection import criteria_signature
            if request.get('inspection_criteria_sha256')!=criteria_signature(root):raise ValueError('PC와 Pi의 안착 검사 기준이 다릅니다. 기준 자료를 동기화한 뒤 전송하세요.')
            for name in ('roi_reference.json','shape_templates.json','shape_templates.npz','appearance_reference.json'):
                if not (Path(root)/'inspection'/name).is_file():raise ValueError('Pi 안착 검사 기준 파일 없음: '+name)
    if preserve_id:
        checked_id(episode['id'])
        return data,cal,recipes,revision,episode
    source_id = episode['id']
    episode['id'] = request['transfer_id']
    episode['pi_export'] = {'source_episode_id': source_id,
                            'source_sha256': digest(request['episode'])}
    return data, cal, recipes, revision, episode


@contextmanager
def execution_lock(root):
    # Shared with the existing Pi-local episode runner and service installer.
    with (Path(root) / 'data/episode-cli.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Pi에서 작업이 실행 중입니다. 작업 종료 후 전송하세요.') from None
        yield


def execution_settings_after(before, arm, draft):
    after=deepcopy(before)
    if draft is not None:
        keys={'speed','acquisition_seconds','acquisition_attempts','hold_seconds'}
        if not isinstance(draft,dict) or set(draft)!=keys or any(type(v) not in (int,float) or not math.isfinite(v) for v in draft.values()):raise ValueError('Pi 실행 설정 형식 오류')
        if draft['speed'] not in (300,350,400) or not 3<=draft['acquisition_seconds']<=10 or not 1<=draft['hold_seconds']<=10 or type(draft['acquisition_attempts']) is not int or not 1<=draft['acquisition_attempts']<=5:raise ValueError('Pi 실행 설정 범위 오류')
        after[arm].update(draft)
    return after


def deliver(root, request, *, check_only=False):
    from so101_teach.domain import atomic_json, read_json, EpisodeStore
    root = Path(root)
    with execution_lock(root):
        data, cal, recipes, revision, episode = validated_episode(root, request)
        arm, slot, transfer = request['arm'], request['slot'], request['transfer_id']
        settings_path=root/'integration/execution-settings.json';settings_before=read_json(settings_path)
        settings_after=execution_settings_after(settings_before,arm,request.get('execution_settings'))
        previous = deepcopy(recipes['recipes'][arm].get(slot))
        expected = {**(previous or {}), 'episode_id': episode['id'], 'name': episode['name'],
                    'timeout_seconds': request['timeout_seconds']}
        path = data / 'episodes' / (episode['id'] + '.json')
        backup = root / 'data/episode-transfer-backups' / transfer
        receipt = {'arm': arm, 'slot': slot, 'episode_id': episode['id'], 'name': episode['name'],
                   'step_count': len(episode['steps']), 'backup_directory': str(backup),
                   'source_episode_id': request['episode']['id']}
        if path.exists():
            if digest(read_json(path)) != digest(episode):
                raise ValueError('같은 전송 ID에 다른 파일이 있습니다. 다시 조회하세요.')
            if previous == expected:
                if settings_before!=settings_after:raise ValueError('전송 이후 Pi 실행 설정이 변경되었습니다. 다시 조회하세요.')
                return {**receipt, 'already_applied': True}
        if request.get('expected_revision') != revision:
            raise ValueError('조회 이후 Pi 실행 연결 또는 설정이 바뀌었습니다. 다시 조회하세요.')
        if check_only:
            return {**receipt, 'check_only': True}
        backup.mkdir(parents=True, exist_ok=True)
        manifest = backup / 'transfer.json'
        if manifest.exists():
            if read_json(manifest)['request_sha256'] != digest(request):
                raise ValueError('전송 기록과 요청이 다릅니다.')
        else:
            atomic_json(backup / 'recipes-before.json', recipes)
            atomic_json(backup / 'execution-settings-before.json',settings_before)
            if previous:
                prior_path = data / 'episodes' / (checked_id(previous['episode_id']) + '.json')
                if prior_path.exists():
                    atomic_json(backup / 'previous-episode.json', read_json(prior_path))
            atomic_json(manifest, {'request_sha256': digest(request), 'created_at': time.time(),
                                   'previous': previous, 'target': expected, **receipt})
        # A new immutable ID keeps old slots and running jobs independent of edits.
        store = EpisodeStore(data / 'episodes', cal, arm)
        store.save(episode)
        if digest(store.load(path)) != digest(episode):
            raise ValueError('전송 파일 확인에 실패했습니다. A/B 연결은 변경하지 않았습니다.')
        updated = deepcopy(recipes)
        updated['recipes'][arm][slot] = expected
        try:
            if settings_after!=settings_before:atomic_json(settings_path,settings_after)
            atomic_json(root / 'integration/recipes.json', updated)
            if read_json(root / 'integration/recipes.json')['recipes'][arm][slot] != expected or read_json(settings_path)!=settings_after:
                raise ValueError('Pi 연결·실행 설정 확인에 실패했습니다.')
        except Exception:
            atomic_json(settings_path,settings_before);atomic_json(root/'integration/recipes.json',recipes)
            raise
        atomic_json(backup / 'receipt.json', receipt)
        return receipt


def store_episode(root, request, *, check_only=False):
    """Update one library episode under the execution lock; keep recipes/settings."""
    from so101_teach.domain import atomic_json,read_json,EpisodeStore
    root=Path(root)
    with execution_lock(root):
        data,cal,recipes,revision,episode=validated_episode(root,request,preserve_id=True)
        path=data/'episodes'/(episode['id']+'.json')
        before=read_json(path) if path.exists() else None
        current=digest(before) if before is not None else None
        backup=root/'data/episode-transfer-backups'/checked_id(request['transfer_id'])
        settings_path=root/'integration/execution-settings.json';settings_before=read_json(settings_path)
        settings_after=execution_settings_after(settings_before,request['arm'],request.get('execution_settings'))
        recipes_after=deepcopy(recipes)
        if request.get('execution_settings') is not None:
            for value in recipes_after['recipes'][request['arm']].values():
                if value['episode_id']==episode['id']:value['timeout_seconds']=request['timeout_seconds']
        receipt_path=backup/'receipt.json';manifest=backup/'transfer.json'
        receipt={'arm':request['arm'],'episode_id':episode['id'],'name':episode['name'],
                 'step_count':len(episode['steps']),'backup_directory':str(backup),
                 'settings_applied':request.get('execution_settings') is not None,
                 'linked_slots':[key for key,value in recipes['recipes'][request['arm']].items() if value['episode_id']==episode['id']]}
        if manifest.exists():
            if read_json(manifest)['request_sha256']!=digest(request):raise ValueError('같은 전송 ID의 내용이 다릅니다. 다시 조회하세요.')
            if current==digest(episode):
                if request.get('execution_settings') is not None and (settings_before!=settings_after or recipes!=recipes_after):raise ValueError('전송 이후 실행 설정이 바뀌었습니다. 다시 조회하세요.')
                return {**receipt,'already_applied':True}
        if request.get('expected_revision')!=revision or request.get('expected_episode_sha256')!=current:
            raise ValueError('조회 이후 Pi 에피소드 또는 실행 설정이 바뀌었습니다. 다시 조회하세요.')
        if check_only:return {**receipt,'check_only':True}
        backup.mkdir(parents=True,exist_ok=True)
        if not manifest.exists():
            if before is not None:atomic_json(backup/'previous-episode.json',before)
            atomic_json(backup/'execution-settings-before.json',settings_before);atomic_json(backup/'recipes-before.json',recipes)
            atomic_json(manifest,{'request_sha256':digest(request),'created_at':time.time(),**receipt})
        store=EpisodeStore(data/'episodes',cal,request['arm'])
        try:
            store.save(episode)
            if digest(store.load(path))!=digest(episode):raise ValueError('Pi 에피소드 저장 확인 실패')
            if settings_after!=settings_before:atomic_json(settings_path,settings_after)
            if recipes_after!=recipes:atomic_json(root/'integration/recipes.json',recipes_after)
            if read_json(settings_path)!=settings_after or read_json(root/'integration/recipes.json')!=recipes_after:raise ValueError('실행 설정 저장 확인 실패')
            atomic_json(receipt_path,receipt)
        except Exception:
            if settings_after!=settings_before:atomic_json(settings_path,settings_before)
            if recipes_after!=recipes:atomic_json(root/'integration/recipes.json',recipes)
            if before is not None:atomic_json(path,before)
            elif path.exists():path.unlink()
            raise
        return receipt


def main():
    try:
        root = Path(sys.argv[1]).expanduser().resolve()
        if not (root / 'integration/episode_cli.py').is_file():
            raise ValueError('Pi 프로그램 폴더에 에피소드 실행기가 없습니다.')
        sys.path.insert(0, str(root))
        raw = sys.stdin.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('전송 에피소드가 너무 큽니다.')
        request = json.loads(raw)
        if request.get('operation') == 'inspect':
            value = inspect_target(root, request['arm'])
        elif request.get('operation') == 'pull':
            value = pull_target(root, request['arm'])
        elif request.get('operation') in ('store_episode','check_episode'):
            value=store_episode(root,request,check_only=request['operation']=='check_episode')
        elif request.get('operation') in ('deliver', 'check'):
            value = deliver(root, request, check_only=request['operation'] == 'check')
        else:
            raise ValueError('지원하지 않는 에피소드 전송 작업입니다.')
        print(json.dumps({'ok': True, 'value': value}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
