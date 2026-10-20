"""The supervision contract stays IDLE/ERROR; detailed events remain local."""
import json
from pathlib import Path
import socket
import time
import os
import tempfile
import uuid
import fcntl


def record_notice(root, arm, status):
    """Persist independent notifications without changing the active job status."""
    folder=Path(root)/'data/episode-cli-runs';folder.mkdir(parents=True,exist_ok=True)
    key=uuid.uuid4().hex
    record={'run_id':key,'events':[{'run_id':key,'arm':arm,'status':status,'at':time.time()}]}
    fd,name=tempfile.mkstemp(dir=folder,prefix='.notice-')
    try:
        with os.fdopen(fd,'w') as stream:json.dump(record,stream,ensure_ascii=False)
        os.replace(name,folder/(key+'.json'))
    finally:
        Path(name).unlink(missing_ok=True)


def status_word(success):
    return 'IDLE' if success else 'ERROR'


def job_succeeded(code, last, check_only=False, *, chained=False, home=False):
    if check_only:return code==0 and last.startswith('CHECK_OK ')
    final=('HOME_RESET_DONE',) if home else ('A_CHAIN_DONE','B_CHAIN_DONE') if chained else ('A_DONE','B_DONE','SLIDE_DONE')
    return code==0 and last in final


def detail_failed(message):
    return '_FAILED:' in message or '_REJECTED:' in message or message.startswith(('ERROR:', 'REJECTED:'))


def feedback_word(detail):
    if detail_failed(detail): return 'ERROR'
    if detail.endswith('_PAUSED'): return 'PAUSED'
    if detail.endswith('_RESUMED'): return 'RUNNING'
    if detail in ('A_DONE', 'B_DONE', 'SLIDE_DONE', 'A_CHAIN_DONE', 'B_CHAIN_DONE','HOME_RESET_DONE'): return 'IDLE'
    return None


def control_state(root):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(Path(root) / 'data/episode-cli.sock'))
        client.sendall(b'inspect')
        raw = b''
        while b'\n' not in raw and len(raw) < 8192:
            part = client.recv(4096)
            if not part: break
            raw += part
    return json.loads(raw)


def clear_paused_job(root, job_lock, timeout=10):
    """End a paused executor; never reset poses, resume, or start a new job.

    Success requires both executor and Action reservations to be released.
    Missing sockets alone never prove idle. The caller checks linear completion.
    """
    try:
        state = control_state(root)
    except (FileNotFoundError, ConnectionRefusedError):
        state = None
    if state is not None:
        if not state.get('paused') or not state.get('info'):
            return False
        # The executor checks paused/control-pending again atomically.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(Path(root) / 'data/episode-cli.sock'))
            key = state['info'].get('run_id', '')
            if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
                return False
            client.sendall(('clear_error ' + key).encode())
            reply = client.recv(8192).decode().strip()
        if reply != 'CLEAR_ERROR_REQUESTED':
            return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job_lock.acquire(blocking=False):
            try:
                with (Path(root) / 'data/episode-cli.lock').open('a') as reservation:
                    try: fcntl.flock(reservation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError: pass
                    else: return True
            finally:
                job_lock.release()
        time.sleep(.05)
    return False


def job_events(root, info):
    key = info['run_id']
    if len(key) != 32 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid run id')
    path = Path(root) / 'data/episode-cli-runs' / (key + '.json')
    if not path.exists(): return []
    data = json.loads(path.read_text())
    if data.get('run_id') != key: raise ValueError('run id changed')
    return data['events']


def wait_control(root, command, info, after, timeout=None):
    wanted = {'estop': 'PAUSED', 'restart': 'RESUMED', 'reset': 'RESET_DONE'}[command]
    deadline = time.monotonic() + (timeout if timeout is not None else 300 if command == 'reset' else 25)
    while time.monotonic() < deadline:
        # Repeated ESTOP can confirm an already-paused job without a new event.
        if command == 'estop':
            try:
                state = control_state(root)
                if (state.get('info') or {}).get('run_id') == info['run_id'] and state.get('paused'):
                    return True
            except (OSError, ValueError): pass
        for event in job_events(root, info)[after:]:
            text = event['status']
            if text.endswith('_' + wanted): return True
            if detail_failed(text) or text in ('A_DONE', 'B_DONE', 'SLIDE_DONE') or text.endswith('_RESET_DONE'):
                return False
        time.sleep(.05)
    return False
