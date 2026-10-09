"""Saved SSH settings and a read-only probe; runtime adapters live in remote_client."""
import ipaddress
import re
import subprocess
from pathlib import Path
from .domain import atomic_json,read_json

DEFAULTS={'host':'','user':'','port':22,'identity_file':'',
          'app_dir':'~/OpenCV_teach','python':'python3'}
PROBE_COMMAND="printf 'SO101_SSH_OK\\n'; uname -s; uname -m"

def validate(values,require_address=False):
    d={k:str(values.get(k,v)).strip() for k,v in DEFAULTS.items()}
    host=d['host']
    if host:
        try:ipaddress.ip_address(host)
        except ValueError:
            if len(host)>253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',p) for p in host.split('.')):
                raise ValueError('Pi 주소에는 IP 또는 호스트 이름만 입력하세요. URL·사용자명은 제외합니다.')
    if d['user'] and not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]{0,63}',d['user']):raise ValueError('Pi 사용자 계정을 확인하세요.')
    if require_address and (not host or not d['user']):raise ValueError('Pi 주소와 사용자 계정을 입력하세요.')
    try:d['port']=int(d['port'])
    except ValueError:raise ValueError('SSH 포트는 1~65535의 정수입니다.') from None
    if not 1<=d['port']<=65535:raise ValueError('SSH 포트는 1~65535의 정수입니다.')
    for key in ('identity_file','app_dir','python'):
        if any(ord(c)<32 for c in d[key]):raise ValueError('경로에 줄바꿈이나 제어 문자를 사용할 수 없습니다.')
    if not d['app_dir'].startswith(('/','~/')):raise ValueError('Pi 프로그램 폴더는 / 또는 ~/로 시작하세요.')
    if not (d['python'].startswith('/') or d['python']=='python3'):raise ValueError('Pi Python은 python3 또는 /로 시작하는 실행 파일 경로를 입력하세요.')
    if d['identity_file']:
        key=Path(d['identity_file']).expanduser()
        if not key.is_file():raise ValueError('선택한 SSH 개인 키 파일을 찾을 수 없습니다.')
        d['identity_file']=str(key.resolve())
    return d

def load(data_dir):
    path=Path(data_dir)/'pi-connection.json'
    return {**DEFAULTS,**read_json(path)} if path.exists() else dict(DEFAULTS)

def save(data_dir,values):
    data=validate(values)
    atomic_json(Path(data_dir)/'pi-connection.json',data)
    return data

def probe(values):
    d=validate(values,True)
    args=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
          '-o','ClearAllForwardings=yes','-o','ForwardAgent=no','-o','ForwardX11=no',
          '-o','ConnectTimeout=4','-o','ConnectionAttempts=1',
          '-o','ServerAliveInterval=2','-o','ServerAliveCountMax=1','-p',str(d['port'])]
    if d['identity_file']:args+=['-o','IdentitiesOnly=yes','-i',d['identity_file']]
    args+=['-l',d['user'],d['host'],PROBE_COMMAND]
    try:result=subprocess.run(args,capture_output=True,text=True,timeout=8,stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:raise ValueError('SSH 응답 시간이 초과됐습니다. Pi 전원·네트워크·주소를 확인하세요.') from None
    except FileNotFoundError:raise ValueError('이 PC에서 SSH 프로그램을 찾을 수 없습니다.') from None
    if result.returncode or 'SO101_SSH_OK' not in result.stdout.splitlines():
        error=result.stderr.lower()
        if 'host key' in error or 'identification has changed' in error:
            raise ValueError('SSH 호스트 키 확인이 필요합니다. 터미널에서 해당 Pi의 신원을 확인하고 등록한 뒤 다시 확인하세요.')
        if 'permission denied' in error:raise ValueError('SSH 인증에 실패했습니다. Pi 계정과 등록된 SSH 키를 확인하세요. 이 화면은 비밀번호를 저장하지 않습니다.')
        raise ValueError('SSH 연결에 실패했습니다. Pi 주소·포트·SSH 서비스·네트워크를 확인하세요.')
    lines=result.stdout.splitlines();start=lines.index('SO101_SSH_OK')
    return {'target':f"{d['user']}@{d['host']}:{d['port']}",'system':' / '.join(lines[start+1:start+3])[:100]}
