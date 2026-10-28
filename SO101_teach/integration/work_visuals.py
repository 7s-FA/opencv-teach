"""Bounded display evidence from existing inspections; no device or decision API."""
import base64
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import uuid

ROW_FIELDS=('id','label','state','quality','reason','product','product_certain','score','seating',
    'roi_polygon','roi_display_polygon','observed_polygon','part_top_polygon',
    'center_error_mm','center_tolerance_mm','rotation_error_deg','rotation_tolerance_deg',
    'insert_center_error_mm','insert_center_tolerance_mm','ai_label')


def scalar(value):
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):return value if math.isfinite(value) else None
    if hasattr(value,'tolist'):return scalar(value.tolist())
    if isinstance(value,(list,tuple)):return [scalar(v) for v in value]
    if isinstance(value,dict):return {str(k):scalar(v) for k,v in value.items()}
    return None


class WorkVisuals:
    """One replaceable frame and bounded per-check metadata, at most 2 writes/s.

    The inspection worker hands over its already classified frame. JPEG encoding
    and disk I/O run separately; errors never change a check or its deadline.
    """
    def __init__(self,data_dir,*,arm,job_id,run_id,episode_id):
        if arm not in ('arm2','arm3'):raise ValueError('Unknown monitor arm')
        self.path=Path(data_dir)/('work-visual-'+arm+'.json')
        self.identity={'schema':1,'arm':arm,'job_id':job_id,'run_id':run_id,
                       'episode_id':episode_id,'instance':uuid.uuid4().hex}
        self.jobs=queue.Queue(1);self.stop=threading.Event();self.thread=None
        self.lock=threading.Lock();self.checks={};self.sequence=0;self.error=None

    def submit(self,frame,at,checks):
        if self.stop.is_set() or type(at) not in (int,float) or not math.isfinite(at):return
        try:
            now=time.monotonic();wall=time.time();rows=[]
            if not 0<=now-at<30:return
            for item in checks:
                if not isinstance(item.get('step_id'),str):continue
                row=item.get('row') or {}
                rows.append({'step_id':item['step_id'],'step_name':item.get('step_name',''),
                    'check':deepcopy(item.get('check') or {}),'product':item.get('product'),
                    'captured_at':at,'captured_wall_time':wall-(now-at),
                    'reason':item.get('reason',''),'row':scalar({k:row[k] for k in ROW_FIELDS if k in row})})
            if not rows:return
            with self.lock:
                if self.stop.is_set():return
                for item in rows:
                    old=self.checks.get(item['step_id'])
                    if old is None or old['captured_at']<=at:self.checks[item['step_id']]=item
                # Keep the newest observations, rather than allowing a delayed
                # worker to replace a newer frame from a parallel check.
                if getattr(self,'latest_at',float('-inf'))>at:return
                self.latest_at=at
                if len(self.checks)>40:
                    keys=sorted(self.checks,key=lambda k:self.checks[k]['captured_at'],reverse=True)[:40]
                    self.checks={k:self.checks[k] for k in keys}
                job=(frame.copy(),at,[r['step_id'] for r in rows],deepcopy(self.checks))
                try:self.jobs.get_nowait()
                except queue.Empty:pass
                self.jobs.put_nowait(job)
                if self.thread is None:
                    self.thread=threading.Thread(target=self.run,daemon=True,name='work-visual-writer');self.thread.start()
        except Exception as exc:self.error=str(exc)

    def run(self):
        while not self.stop.is_set() or not self.jobs.empty():
            try:job=self.jobs.get(timeout=.1)
            except queue.Empty:continue
            began=time.monotonic()
            try:self.write(*job)
            except Exception as exc:self.error=str(exc)
            self.stop.wait(max(0.,.5-(time.monotonic()-began)))

    def write(self,frame,at,keys,checks):
        import cv2
        height,width=frame.shape[:2];scale=min(1.,960/width)
        preview=cv2.resize(frame,(round(width*scale),round(height*scale))) if scale<1 else frame
        ok,jpeg=cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,82])
        if not ok:raise ValueError('판정 영상 JPEG 생성 실패')
        self.sequence+=1
        value={**self.identity,'sequence':self.sequence,'captured_at':at,
            'published_at':time.time(),'source_size':[width,height],
            'image':base64.b64encode(jpeg).decode(),'frame_checks':keys,'checks':checks}
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(raw.encode())>2*1024*1024:raise ValueError('판정 영상 크기 제한 초과')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.'+self.identity['instance']+'.tmp')
        try:
            temporary.write_text(raw);os.replace(temporary,self.path)
        finally:temporary.unlink(missing_ok=True)

    def close(self):
        self.stop.set()
        if self.thread and self.thread is not threading.current_thread():self.thread.join(1.)
