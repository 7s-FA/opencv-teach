"""Bounded image-only subprocess protocol for PC TensorRT and Pi NCNN."""
from collections import deque
import json
import os
from pathlib import Path
import queue
import shlex
import struct
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
CONFIDENCES=(.5,.6,.7,.8,.9)
MAX_IMAGE_BYTES=32*1024*1024
PC_MAX_FPS=10.  # Match the shared camera's active preview rate.

def confidence_value(value):
    number=float(value)
    if number not in CONFIDENCES:raise ValueError('최소 신뢰도는 0.5~0.9에서 0.1 단위로 선택하세요.')
    return number

from .inference_protocol import read_exact,send_packet,receive_packet

def worker_command(model_path,*,backend,python,remote=None,remote_dir=None):
    if backend not in ('ncnn','tensorrt','sam3'):raise ValueError('지원하지 않는 인식 모델입니다.')
    args=[str(python),'-u','-m','so101_teach.yolo_worker','--serve','--backend',backend,'--model',str(model_path)]
    if remote is None:return args
    from .remote_client import ssh_args
    if not remote_dir or not str(remote_dir).startswith('/'):raise ValueError('Pi YOLO 실행 폴더를 확인하세요.')
    args+=['--codec','png']
    command='cd '+shlex.quote(str(remote_dir))+' && exec env OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=4 '+shlex.join(args)
    return ssh_args(remote)+['-o','ControlMaster=no','-o','ControlPath=none','-l',remote['user'],remote['host'],command]

class YoloWorker:
    def __init__(self):
        self.process=None;self.requests=None;self.results=None;self.stop_event=None
        self.ready=False;self.busy=False;self.started_at=None;self.submitted_at=None
        self.stderr=deque(maxlen=12);self.model=None
        self.max_fps=None;self.next_submit_at=0.
        self.startup_timeout=60

    def start(self,model_path,*,backend='ncnn',python=None,remote=None,remote_dir=None,expected_manifest=None):
        self.close();self.stderr=deque(maxlen=12)
        command=worker_command(model_path,backend=backend,python=python or sys.executable,remote=remote,remote_dir=remote_dir)
        env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'4','YOLO_AUTOINSTALL':'False'}
        optimization=(expected_manifest or {}).get('runtime_optimization',{})
        self.startup_timeout=300 if backend=='sam3' and optimization.get('torch_compile') else 60
        if backend=='sam3' and optimization.get('torch_compile'):
            env.update(TORCHINDUCTOR_COMPILE_THREADS='1',MAX_JOBS='1')
            if optimization.get('cache_dir'):env['TORCHINDUCTOR_CACHE_DIR']=optimization['cache_dir']
        if backend=='sam3':env.pop('NVIDIA_TF32_OVERRIDE',None)
        if backend=='tensorrt':env['NVIDIA_TF32_OVERRIDE']='0'  # Match the evaluated engine build.
        env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
        process=subprocess.Popen(command,cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        self.process=process;self.requests=queue.Queue(maxsize=1);self.results=queue.Queue(maxsize=2)
        self.max_fps=PC_MAX_FPS if remote is None else None
        self.stop_event=threading.Event();self.started_at=time.monotonic()
        requests,results,stop=self.requests,self.results,self.stop_event;errors=self.stderr
        def stderr_reader():
            try:
                for line in iter(process.stderr.readline,b''):
                    errors.append(line.decode(errors='replace').strip()[-500:])
            except (ValueError,OSError):pass
        def run():
            try:
                ready=receive_packet(process.stdout)
                if ready.get('kind')!='ready':raise RuntimeError(ready.get('error','모델을 시작하지 못했습니다.'))
                if expected_manifest and ready.get('model',{}).get('files')!=expected_manifest.get('files'):
                    raise ValueError('실행부 모델이 선택한 모델과 다릅니다.')
                results.put(ready)
                while not stop.is_set():
                    item=requests.get()
                    if item is None:return
                    frame,at,confidence=item;began=time.monotonic()
                    send_packet(process.stdin,{'kind':'infer','frame_at':at,'confidence':confidence},frame,'png' if remote else 'raw')
                    result=receive_packet(process.stdout)
                    if result.get('kind')=='error':raise RuntimeError(result.get('error','인식 실패'))
                    if result.get('kind')!='result' or result.get('frame_at')!=at or result.get('confidence')!=confidence:
                        raise ValueError('인식 응답이 요청 프레임과 다릅니다.')
                    if result['image'].shape!=frame.shape:raise ValueError('인식 결과 크기가 원본과 다릅니다.')
                    result.update(raw=frame,finished_at=time.monotonic(),roundtrip_ms=(time.monotonic()-began)*1000,
                                  model=ready['model'],runtime=ready.get('runtime'))
                    if not stop.is_set():results.put(result)
            except Exception as exc:
                if not stop.is_set():
                    detail=' · '+errors[-1] if errors and isinstance(exc,EOFError) else ''
                    try:results.put_nowait({'kind':'error','error':str(exc)+detail})
                    except queue.Full:pass
        threading.Thread(target=stderr_reader,daemon=True).start()
        threading.Thread(target=run,daemon=True).start()

    def submit(self,frame,frame_at,confidence):
        confidence=confidence_value(confidence)
        if not self.ready or self.busy:return False
        now=time.monotonic()
        if now<self.next_submit_at:return False
        try:self.requests.put_nowait((frame.copy(),frame_at,confidence))
        except queue.Full:return False
        self.next_submit_at=now+1/self.max_fps if self.max_fps else now
        self.busy=True;self.submitted_at=now;return True

    def poll(self):
        if self.process is None:return None
        try:result=self.results.get_nowait()
        except queue.Empty:
            if self.process.poll() is not None:
                detail=' · '+self.stderr[-1] if self.stderr else ''
                return {'kind':'error','error':'인식 실행부가 종료되었습니다.'+detail}
            since=self.submitted_at if self.busy else self.started_at if not self.ready else None
            limit=30 if self.ready else self.startup_timeout
            if since is not None and time.monotonic()-since>limit:
                return {'kind':'error','error':f'모델 응답 시간이 {limit}초를 넘었습니다. 연결과 실행 환경을 확인하세요.'}
            return None
        if result['kind']=='ready':self.ready=True;self.model=result['model']
        elif result['kind']=='result':self.busy=False;self.submitted_at=None
        return result

    def close(self):
        if self.stop_event:self.stop_event.set()
        if self.requests:
            try:self.requests.get_nowait()
            except queue.Empty:pass
            try:self.requests.put_nowait(None)
            except queue.Full:pass
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(.2)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(.2)
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):
                try:stream.close()
                except OSError:pass
        self.process=None;self.requests=None;self.results=None;self.stop_event=None
        self.ready=False;self.busy=False;self.started_at=None;self.submitted_at=None;self.model=None
        self.max_fps=None;self.next_submit_at=0.

def serve(backend,model_path,codec,regions_only=False):
    # Preserve a clean binary channel even if native libraries print to stdout.
    output=os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    try:
        from .yolo_inference import Segmenter,TensorRTSegmenter,Sam3Segmenter,CLASS_LABELS
        model={'tensorrt':TensorRTSegmenter,'ncnn':Segmenter,'sam3':Sam3Segmenter}[backend](model_path,threads=4)
        import platform
        runtime={'host':platform.node(),'machine':platform.machine(),'pid':os.getpid(),'backend':backend,'python':sys.version.split()[0],
                 'optimization':model.manifest.get('runtime_optimization')}
        if regions_only:
            import hashlib
            runtime['provider_sha256']={name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                                       for name in ('yolo_worker.py','yolo_inference.py','inference_protocol.py')}
        if backend in ('tensorrt','sam3'):
            import torch
            runtime['device']=torch.cuda.get_device_name(0)
        else:runtime.update(device='CPU',threads=4)
        if backend=='sam3' and (runtime['optimization'] or {}).get('torch_compile'):
            from torch._dynamo.utils import counters
            runtime.update(compiled_graphs=counters['stats']['unique_graphs'],text_cache_size=model.text_cache_size)
            if runtime['compiled_graphs']<1:raise RuntimeError('SAM3 컴파일 실행을 확인하지 못했습니다.')
        send_packet(output,{'kind':'ready','model':model.manifest,'backend':backend,'runtime':runtime})
        while True:
            try:item=receive_packet(sys.stdin.buffer)
            except EOFError:return
            if item.get('kind')!='infer':raise ValueError('인식 요청만 허용됩니다.')
            confidence=confidence_value(item['confidence']);model.conf=confidence;frame=item['image']
            result=model.infer(frame)
            rows=[{'class_id':int(c),'class_name':model.names[int(c)],'label':CLASS_LABELS[int(c)],
                   'confidence':float(s),'box':box.tolist(),'mask_pixels':int(mask.sum())}
                  for c,s,box,mask in zip(result['classes'],result['scores'],result['boxes'],result['masks'])]
            if regions_only:
                from .inference_protocol import encode_mask
                classes=item.get('mask_classes',[0,1])
                if not isinstance(classes,list) or not classes or len(classes)>13 or any(type(c) is not int or not 0<=c<13 for c in classes):raise ValueError('지원하지 않는 마스크 클래스 요청')
                pairs=[(row,mask) for row,mask in zip(rows,result['masks']) if row['class_id'] in classes]
                if len(pairs)*frame.shape[0]*frame.shape[1]>MAX_IMAGE_BYTES:raise ValueError('지그 마스크 응답이 너무 큽니다.')
                rows=[{**row,'mask':encode_mask(mask)} for row,mask in pairs]
            send_packet(output,{'kind':'result','frame_at':item['frame_at'],'confidence':confidence,
                               **({'mask_shape':list(frame.shape[:2])} if regions_only else {}),
                               'detections':rows,'inference_ms':result['inference_ms'],'total_ms':result['total_ms']},None if regions_only else model.draw(frame,result),codec)
    except Exception as exc:
        try:send_packet(output,{'kind':'error','error':type(exc).__name__+': '+str(exc)})
        except (BrokenPipeError,OSError):pass
    finally:output.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true',required=True)
    parser.add_argument('--backend',choices=('ncnn','tensorrt','sam3'),required=True);parser.add_argument('--model',required=True)
    parser.add_argument('--codec',choices=('raw','png'),default='raw');parser.add_argument('--regions',action='store_true');args=parser.parse_args()
    serve(args.backend,args.model,args.codec,args.regions)
