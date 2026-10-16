"""Read archive files locally or through the existing SSH configuration."""
import json
from pathlib import Path
import shlex
import subprocess
from . import failure_archive


def fetch(data_dir,source,message,config=None):
    if source=='local':return failure_archive.request(data_dir,message)
    from .pi_connection import validate
    from .remote_client import ssh_args
    config=validate(config or {},True)
    script=Path(failure_archive.__file__).read_text()
    command=' '.join(shlex.quote(v) for v in (config['python'],'-B','-c',script,config['app_dir']))
    try:
        result=subprocess.run(ssh_args(config)+['-l',config['user'],config['host'],command],input=json.dumps(message),capture_output=True,text=True,timeout=15)
    except subprocess.TimeoutExpired:raise ValueError('Pi 기록 조회 시간이 초과됐습니다. 연결을 확인한 뒤 새로고침하세요.') from None
    except FileNotFoundError:raise ValueError('SSH 프로그램을 찾을 수 없습니다.') from None
    if result.returncode:raise ValueError('Pi 오류 기록을 읽지 못했습니다. 주소·네트워크·SSH 연결 설정을 확인하세요.')
    value=json.loads(result.stdout)
    if not value.get('ok'):raise ValueError(value.get('error','Pi 기록 조회 실패'))
    return value['value']
