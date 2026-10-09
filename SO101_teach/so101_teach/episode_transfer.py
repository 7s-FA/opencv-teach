"""Snapshot validation and bounded SSH transport for Pi A/B episode delivery."""
from copy import deepcopy
import json
import math
from pathlib import Path
import shlex
import subprocess
import uuid
from .arm_workspace import arm_id, require_calibrated
from .pi_connection import validate
from .remote_client import ssh_args


def episode_snapshot(app):
    require_calibrated(app.profile)
    episode = deepcopy(app.episode)
    episode['name'] = app.episode_name.get().strip()
    app.store.validate(episode)
    if not episode['steps']:
        raise ValueError('저장된 스텝이 없습니다. 스텝을 추가한 뒤 전송하세요.')
    from .episode_inspection import inspection_schedule
    inspection_schedule(episode['steps'])
    episode['robot_id'] = arm_id(app.profile)
    return episode


def delivery_request(app, slot, timeout, target):
    episode = episode_snapshot(app)
    if slot not in ('A', 'B'):
        raise ValueError('A 또는 B 실행을 선택하세요.')
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        raise ValueError('실행 제한시간은 1~86400초입니다.') from None
    if not math.isfinite(timeout) or not 1 <= timeout <= 86400:
        raise ValueError('실행 제한시간은 1~86400초입니다.')
    if target['arm'] != episode['robot_id']:
        raise ValueError('조회한 Pi 로봇팔과 현재 선택한 팔이 다릅니다. 다시 조회하세요.')
    if target['calibration_sha256'] != episode['calibration_sha256']:
        raise ValueError('PC 에피소드와 Pi 영점이 다릅니다. 같은 영점 설정인지 확인하세요.')
    from .episode_inspection import has_inspections,criteria_signature
    inspection={'inspection_criteria_sha256':criteria_signature()} if has_inspections(episode) else {}
    return {**inspection,'operation': 'deliver', 'arm': episode['robot_id'], 'slot': slot,
            'timeout_seconds': float(timeout), 'transfer_id': uuid.uuid4().hex,
            'expected_revision': target['revision'], 'episode': episode,
            **({'execution_settings':deepcopy(episode['pi_execution_settings'])} if 'pi_execution_settings' in episode else {})}


def episode_delivery_request(app, target, *, apply_settings=False):
    # Reuse arm/calibration/inspection validation without choosing an A/B slot.
    request=delivery_request(app,'A',app.episode.get('pi_timeout_seconds',900),target)
    request.pop('slot')
    if not apply_settings:request.pop('execution_settings',None)
    request['operation']='store_episode'
    request['expected_episode_sha256']=target.get('episodes',{}).get(request['episode']['id'],{}).get('sha256')
    return request


def remote_request(config, request):
    values = validate(config, True)
    script = Path(__file__).with_name('episode_transfer_receiver.py').read_text(encoding='utf-8')
    command = ' '.join(shlex.quote(value) for value in (values['python'], '-B', '-c', script, values['app_dir']))
    args = ssh_args(values) + ['-l', values['user'], values['host'], command]
    try:
        result = subprocess.run(args, input=json.dumps(request, ensure_ascii=False, allow_nan=False),
                                capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        raise ValueError('Pi 응답 시간이 초과됐습니다. 전송 중이었다면 실행 연결을 다시 조회해 적용 여부를 확인하세요.') from None
    except FileNotFoundError:
        raise ValueError('SSH 프로그램을 찾을 수 없습니다.') from None
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise ValueError('Pi 연결 또는 응답 확인에 실패했습니다. 주소·SSH 설정을 확인하고 실행 연결을 다시 조회하세요.') from None
    if not response.get('ok'):
        raise ValueError(response.get('error', 'Pi 에피소드 전송 실패'))
    if result.returncode:
        raise ValueError('Pi 처리 종료를 확인하지 못했습니다. 실행 연결을 다시 조회하세요.')
    return response['value']
