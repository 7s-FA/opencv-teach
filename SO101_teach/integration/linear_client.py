"""Local preset controller client, independent of the CLI's Python/ROS runtime."""
import json
from pathlib import Path
import socket
import threading
import uuid


class LinearClient:
    def __init__(self):
        self.path = Path.home() / '.local/state/arm3-linear-controller/control.sock'
        self.owner = uuid.uuid4().hex
        self.error = None
        self.stop = threading.Event()
        self.claimed = False

    def call(self, op, **args):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(self.path))
            client.sendall((json.dumps({'op': op, 'owner': self.owner, **args}) + '\n').encode())
            raw = b''
            while b'\n' not in raw and len(raw) < 65536:
                part = client.recv(8192)
                if not part: break
                raw += part
        result = json.loads(raw)
        if not result['ok']: raise RuntimeError(result['error'])
        return result['value']

    def open(self):
        self.call('claim')
        self.claimed = True
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
        self.thread.start()

    def heartbeat(self):
        while not self.stop.wait(.3):
            try:
                value = self.call('status')
                if not value.get('owner_matches'):
                    raise RuntimeError('리니어 사용권이 만료되었거나 변경되었습니다.')
            except Exception as exc:
                self.error = str(exc)
                return

    def close(self):
        if self.claimed:
            try: self.call('release')
            finally:
                self.stop.set(); self.thread.join(2)
