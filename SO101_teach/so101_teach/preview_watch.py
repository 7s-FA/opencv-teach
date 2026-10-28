"""Camera-only viewers yield to device owners; they never acquire robot leases."""
import json
import re
import time
from pathlib import Path
from urllib.request import Request,urlopen

TTL_SECONDS=4.


class PreviewWatch:
    def __init__(self,runtime):
        self.runtime=runtime;self.viewers={};self.owned=None;self.acquiring=False

    def status(self):
        r=self.runtime
        return {'controlling':bool(self.acquiring or r.lease and not r.detached),
                'passive_preview':bool(self.owned is not None and self.owned.running)}

    def peer(self,operation):
        r=self.runtime
        if r.robot_id not in ('arm2','arm3'):return {}
        shared=r.data_dir.parent if r.data_dir.name=='arm3-runtime' else r.data_dir
        folder=shared/'arm3-runtime' if r.robot_id=='arm2' else shared
        meta=folder/'remote-server.json'
        if not meta.exists():return {}
        try:
            info=json.loads(meta.read_text());token=(folder/'remote-token').read_text().strip()
            request=Request('http://127.0.0.1:'+str(info['port'])+'/preview',
                data=json.dumps({'operation':operation}).encode(),headers={'Authorization':'Bearer '+token})
            with urlopen(request,timeout=2.5 if operation=='yield' else .4) as response:reply=json.load(response)
            return reply['value'] if reply.get('ok') else {}
        except (OSError,ValueError,KeyError):
            # A live, unavailable peer is not permission to open its camera.
            return {'controlling':True}

    def yield_owned(self):
        camera=self.owned;self.owned=None
        if camera:
            camera.close()
            if not camera.join(2.):raise ValueError('조회 카메라 정리 중입니다. 잠시 후 다시 연결하세요.')

    def begin_acquire(self):
        # Advertise intent before touching the peer. Never hold either runtime's
        # lock during a cross-runtime request: both arms can connect together.
        self.acquiring=True
        self.peer('yield')
        with self.runtime.lock:self.yield_owned()

    def expire(self):
        now=time.monotonic();self.viewers={key:until for key,until in self.viewers.items() if until>now}
        if not self.viewers and self.owned is not None:
            self.owned.close();self.owned=None

    def call(self,body):
        operation=body.get('operation');r=self.runtime
        if operation=='status':return self.status()
        if operation not in ('watch','release','yield'):raise ValueError('카메라 조회 요청 오류')
        if operation!='yield':
            viewer=body.get('viewer')
            if not isinstance(viewer,str) or re.fullmatch('[0-9a-f]{32}',viewer) is None:raise ValueError('카메라 조회 식별자 오류')
        with r.lock:
            self.expire()
            if operation=='yield':self.yield_owned();return self.status()
            if operation=='release':
                self.viewers.pop(viewer,None);self.expire();return self.status()
            if viewer not in self.viewers and len(self.viewers)>=16:raise ValueError('카메라 조회 연결 수 초과')
            self.viewers[viewer]=time.monotonic()+TTL_SECONDS
            # Running owner cameras are shared without changing their processor,
            # frame rate, frozen poses, mode or lifetime.
            if r.camera and r.camera.running:return self.status()
            if self.status()['controlling'] or self.peer('status').get('controlling'):return self.status()
            profile=r.profile
            if profile is None:
                profile=json.loads((r.data_dir/'profile.json').read_text())
            camera=profile.get('camera') or {}
            if not camera.get('source'):raise ValueError('저장된 카메라 설정 없음')
            # A processor placeholder starts the processing thread, so the owner
            # can later reuse the normal CameraSession lifecycle if needed.
            r.camera=r.camera_factory(**camera,processor=lambda frame:None)
            r.camera.set_mode(processing_enabled=False,preview_fps=10)
            r.camera.max_fps=10;self.owned=r.camera;r.camera.start()
            return self.status()
