"""Encode synchronized original/inspection videos on the PC; no network access."""
from datetime import datetime
from pathlib import Path
from copy import deepcopy
import queue
import json
import math
import threading
import time
import uuid


class PreviewRecording(threading.Thread):
    def __init__(self,folder,*,now=None,fps=10):
        super().__init__(daemon=True,name='work-preview-recording')
        if fps not in (2,5,10):raise ValueError('지원하는 녹화 속도는 2, 5 또는 10 FPS입니다.')
        self.fps=fps
        folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
        self.name='work-live-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        self.path=folder/(self.name+'_original.mp4');self.inspection_path=folder/(self.name+'_inspection.mp4')
        self.partial=self.path.with_suffix('.partial.mp4');self.inspection_partial=self.inspection_path.with_suffix('.partial.mp4')
        self.started=time.monotonic() if now is None else now
        self.last_index=-1;self.frames=0;self.error=None;self.final_index=None
        self.stop_reason=None;self.coalesced_frames=0;self.journal_error=None
        self.journal_path=folder/(self.name+'_recording.jsonl');self.journal=None
        self.jobs=queue.Queue(maxsize=4);self.stopping=threading.Event();self.done=threading.Event()

    def offer(self,image,now,*,inspection=None,source_state='live'):
        index=int(max(0,now-self.started)*self.fps+1e-6)
        if self.stopping.is_set() or self.done.is_set() or index<=self.last_index:return
        self.last_index=index
        # Evaluate the UI scene only for an accepted frame slot, on the UI thread.
        scene=inspection(now) if inspection is not None else None
        if scene is not None:scene={**deepcopy({k:v for k,v in scene.items() if k!='image'}),'image':scene['image'].copy()}
        job=(index,image.copy(),scene,source_state)
        try:self.jobs.put_nowait(job)
        except queue.Full:
            # Keep memory bounded without ending a user-owned recording. The
            # encoder fills skipped slots on the same elapsed-time timeline.
            try:self.jobs.get_nowait();self.coalesced_frames+=1
            except queue.Empty:pass
            self.jobs.put_nowait(job)

    def finish(self,*,reason='직접 종료',now=None):
        if self.stopping.is_set():return
        self.stop_reason=reason
        if now is not None:self.final_index=max(self.last_index,math.ceil(max(0,now-self.started)*self.fps)-1)
        self.stopping.set()

    def log(self,event,**details):
        if self.journal is None:return
        try:
            self.journal.write(json.dumps({'event':event,**details},ensure_ascii=False)+'\n');self.journal.flush()
        except OSError as exc:self.journal_error=str(exc)

    def run(self):
        writers=[];size=None;previous=None;previous_index=-1;source_state=None;waiting=None
        try:
            import cv2
            import numpy as np
            from PIL import Image
            from .recording_visual import render_inspection
            try:self.journal=self.journal_path.open('x',encoding='utf-8')
            except OSError as exc:self.journal_error=str(exc)
            self.log('start',at_unix=time.time(),fps=self.fps,original=self.path.name,inspection=self.inspection_path.name)
            def fill_until(index):
                stale_from=max(self.frames,previous_index+self.fps*2)
                if index>stale_from:self.log('timer_gap',from_seconds=stale_from/self.fps,to_seconds=index/self.fps,fill='previous_frame')
                while self.frames<index:
                    frames=previous if previous is not None else (waiting,waiting)
                    for writer,frame in zip(writers,frames):writer.write(frame)
                    self.frames+=1
            while not self.stopping.is_set() or not self.jobs.empty():
                try:index,image,scene,state=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                if not writers:
                    scale=min(1.,960/image.width)
                    size=tuple(max(2,int(n*scale)//2*2) for n in image.size)
                    for path in (self.partial,self.inspection_partial):
                        writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),self.fps,size);writers.append(writer)
                        if not writer.isOpened():raise OSError('MP4 인코더를 열 수 없습니다: '+path.name)
                    blank=Image.new('RGB',size,'#142d38')
                    waiting=cv2.cvtColor(np.asarray(render_inspection(blank,size,{'image':blank,'placeholder':'영상 수신 대기'})),cv2.COLOR_RGB2BGR)
                if state!=source_state:
                    self.log('source',seconds=index/self.fps,state=state);source_state=state
                inspected=render_inspection(image,size,scene)
                if image.size!=size:image=image.resize(size)
                frames=[cv2.cvtColor(np.asarray(frame.convert('RGB')),cv2.COLOR_RGB2BGR) for frame in (image,inspected)]
                fill_until(index)
                for writer,frame in zip(writers,frames):writer.write(frame)
                self.frames+=1;previous=frames;previous_index=index
            if writers and self.final_index is not None:fill_until(self.final_index+1)
        except Exception as exc:self.error=str(exc);self.stopping.set()
        finally:
            for writer in writers:
                try:writer.release()
                except Exception as exc:self.error=str(exc)
            committed=[]
            try:
                if not self.error:
                    if not self.frames:raise OSError('저장할 영상 프레임이 없습니다.')
                    pairs=((self.partial,self.path),(self.inspection_partial,self.inspection_path))
                    for partial,_ in pairs:
                        if not partial.is_file() or partial.stat().st_size==0:raise OSError('영상 파일을 기록하지 못했습니다: '+partial.name)
                    for partial,path in pairs:partial.replace(path);committed.append((partial,path))
            except Exception as exc:
                self.error=str(exc)
                for partial,path in committed:
                    try:path.replace(partial)
                    except OSError:pass
            self.log('finish',at_unix=time.time(),reason=self.stop_reason or '저장 오류',error=self.error,
                     frames=self.frames,duration_seconds=self.frames/self.fps,coalesced_frames=self.coalesced_frames)
            if self.journal:
                try:self.journal.close()
                except OSError as exc:self.journal_error=str(exc)
            self.done.set()
