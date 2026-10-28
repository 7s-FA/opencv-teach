"""Bounded SSH observation stream. Owns a preview subscription, no robot lease."""
import base64
import io
import json
from pathlib import Path
import queue
import select
import shlex
import subprocess
import threading
import time
from PIL import Image
from so101_teach.pi_connection import validate
from so101_teach.remote_client import ssh_args

class WorkObserver(threading.Thread):
    def __init__(self,config):
        super().__init__(daemon=True,name='work-observer')
        self.config=validate(config,True);self.stop=threading.Event();self.results=queue.Queue(maxsize=1)
        self.process=None;self.image=None;self.image_key=None;self.image_received=None;self.image_age=None
        self.records={};self.events={}
        self.inspection_image=None;self.inspection_key=None;self.preview_fps=10

    def publish(self,value):
        try:self.results.put_nowait(value)
        except queue.Full:
            try:self.results.get_nowait()
            except queue.Empty:pass
            self.results.put_nowait(value)

    def open(self):
        c=self.config;source=Path(__file__).with_name('source.py').read_text()
        command=' '.join(shlex.quote(v) for v in (c['python'],'-u','-B','-c',source,c['app_dir']))
        # A separate SSH stream never acquires a runtime lease or supervises a job.
        self.process=subprocess.Popen([*ssh_args(c),'-o','ControlMaster=no','-o','ControlPath=none',
            '-l',c['user'],c['host'],command],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)

    def disconnect(self):
        process=self.process;self.process=None
        if process:
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=.5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=.5)
            for stream in (process.stdin,process.stdout):
                if stream:stream.close()

    def close(self):
        self.stop.set()
        process=self.process
        if process and process.poll() is None:process.terminate()

    def poll_once(self):
        started=time.monotonic();process=self.process
        process.stdin.write(b'{"operation":"snapshot"}\n');process.stdin.flush()
        if not select.select([process.stdout],[],[],6)[0]:raise TimeoutError('Pi 기록 수신 시간 초과')
        raw=process.stdout.readline(32*1024*1024)
        if not raw or not raw.endswith(b'\n'):raise ConnectionError('Pi 조회 연결이 끊겼습니다.')
        reply=json.loads(raw)
        if not reply.get('ok'):raise ValueError(reply.get('error','Pi 조회 실패'))
        value=reply['value'];received=time.monotonic();camera=value.get('camera',{})
        if value.pop('events_reset',False):self.events.clear();self.records.clear()
        for row in value.pop('events',[]):self.events[(row['run_id'],row['sequence'])]=row
        ordered=sorted(self.events.values(),key=lambda r:(r['at'],r['run_id'],r['sequence']))[-1000:]
        self.events={(r['run_id'],r['sequence']):r for r in ordered}
        for key in ('runs','names','errors'):
            if key in value:self.records[key]=value[key]
        value={**self.records,**value,'events':ordered}
        encoded=camera.pop('image',None);key=(tuple(camera.get('identity',())),camera.get('at'))
        if encoded:
            with Image.open(io.BytesIO(base64.b64decode(encoded,validate=True))) as image:
                if image.width*image.height>4096*2160:raise ValueError('영상 크기 제한 초과')
                self.image=image.convert('RGB')
            self.image_key=key;self.image_received=received;self.image_age=float(camera.get('age_s') or 0)+received-started
        if not camera.get('running') or self.image_key and self.image_key[0]!=key[0]:
            self.image=None;self.image_key=None
        visual=value.get('visual') or {};inspection_key=(visual.get('instance'),visual.get('sequence'))
        encoded=visual.pop('image',None)
        if encoded:
            with Image.open(io.BytesIO(base64.b64decode(encoded,validate=True))) as picture:
                if picture.width*picture.height>4096*2160:raise ValueError('판정 영상 크기 제한 초과')
                self.inspection_image=picture.convert('RGB')
            self.inspection_key=inspection_key
        elif not visual or inspection_key!=self.inspection_key:
            self.inspection_image=None;self.inspection_key=None
        return {**value,'connected':True,'received':received,'round_trip_s':received-started,
                'wire_bytes':len(raw),'frame':self.image,'frame_key':self.image_key,'frame_received':self.image_received,'frame_age':self.image_age,
                'inspection_frame':self.inspection_image,'inspection_key':self.inspection_key}

    def run(self):
        try:
            while not self.stop.is_set():
                began=time.monotonic()
                try:
                    if self.process is None:self.open()
                    value=self.poll_once()
                    if not self.stop.is_set():self.publish(value)
                except Exception as exc:
                    self.disconnect();self.image=None;self.image_key=None;self.inspection_image=None;self.inspection_key=None
                    if not self.stop.is_set():self.publish({'connected':False,'error':str(exc),'received':time.monotonic()})
                    if self.stop.wait(2):break
                self.stop.wait(max(.01,1/self.preview_fps-(time.monotonic()-began)))
        finally:self.disconnect()
