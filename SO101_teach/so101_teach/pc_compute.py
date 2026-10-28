"""Job-bound, lossless PC compute RPC. No camera or actuator operations exist here."""
import base64
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
import uuid
import zlib
import numpy as np
from .domain import ROOT

MAX_BYTES=64*1024*1024
PC_COMMANDS=('PC_BUILD_LOAD_A','PC_BUILD_LOAD_B')
# Hash the actual criteria and geometry implementation, including template data.
CRITERIA=('ai_inspection.py','shape_inspection.py','vision.py','mask_pose.py','model_regions.py',
          'lens_geometry.py','height_reference.py','episode_inspection.py','shared_inference.py',
          'yolo_inference.py','inference_protocol.py')

def contract():
    from .runtime_version import manifest
    hashes=manifest(ROOT)['files']
    # Pi's selected NCNN worker lives in its independent YOLO environment.
    # Bind that actually used provider, not an unused desktop-preview copy.
    from .model_regions import runtime
    provider=runtime('n')[2]['_provider_sha256']
    hashes.update({'selected_provider/'+name:digest for name,digest in provider.items()})
    return hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()


def start_guard(client,done,on_failure):
    """An independent connection checks liveness while inference/motion is pending."""
    probe=ComputeClient(client.descriptor)
    def watch():
        try:
            while not done.wait(.5):
                try:probe.call('hello',timeout=1.5)
                except Exception as exc:
                    if not done.is_set():on_failure(str(exc))
                    return
        finally:probe.close()
    thread=threading.Thread(target=watch,daemon=True,name='pc-compute-health');thread.start()
    return thread


def encode(value):
    if isinstance(value,np.ndarray):
        if value.dtype.kind not in 'buif' or value.nbytes>MAX_BYTES:raise ValueError('Invalid compute array')
        if value.dtype.kind=='f' and not np.isfinite(value).all():raise ValueError('Nonfinite compute array')
        return {'__array__':True,'dtype':value.dtype.str,'shape':list(value.shape),
                'data':base64.b64encode(zlib.compress(value.tobytes(),1)).decode()}
    if isinstance(value,np.generic):return encode(value.item())
    if isinstance(value,dict):return {str(k):encode(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [encode(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):raise ValueError('Nonfinite compute value')
    if value is None or isinstance(value,(str,int,float,bool)):return value
    raise ValueError('Unsupported compute value: '+type(value).__name__)

def decode(value,*,budget=None,depth=0):
    if budget is None:budget=[MAX_BYTES]
    if depth>48:raise ValueError('Compute nesting too deep')
    if isinstance(value,dict):
        if '__array__' in value:
            dtype=np.dtype(value['dtype']);shape=value['shape']
            if dtype.kind not in 'buif' or not isinstance(shape,list) or len(shape)>5 or any(type(n) is not int or n<0 for n in shape):raise ValueError('Invalid compute array')
            size=math.prod(shape)*dtype.itemsize
            if size>budget[0]:raise ValueError('Compute array budget exceeded')
            budget[0]-=size
            packed=base64.b64decode(value['data'],validate=True);stream=zlib.decompressobj()
            raw=stream.decompress(packed,size+1)
            if len(raw)!=size or not stream.eof or stream.unused_data:raise ValueError('Invalid compute array size')
            array=np.frombuffer(raw,dtype=dtype).reshape(shape).copy()
            if dtype.kind=='f' and not np.isfinite(array).all():raise ValueError('Nonfinite compute array')
            return array
        return {k:decode(v,budget=budget,depth=depth+1) for k,v in value.items()}
    if isinstance(value,list):return [decode(v,budget=budget,depth=depth+1) for v in value]
    if isinstance(value,float) and not math.isfinite(value):raise ValueError('Nonfinite compute response')
    return value

def dumps(value):
    return encoded_dumps(encode(value))

def encoded_dumps(value):
    """Canonical bytes of an already encoded tree; arrays are packed once."""
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_BYTES:raise ValueError('Compute packet too large')
    return raw

def loads(raw):
    if len(raw)>MAX_BYTES:raise ValueError('Compute packet too large')
    return decode(json.loads(raw))

def frame_id(frame):
    if frame is None:return None
    if frame.dtype!=np.uint8 or frame.ndim!=3 or frame.shape[2]!=3:raise ValueError('Expected raw BGR frame')
    return hashlib.sha256(str(frame.shape).encode()+frame.tobytes()).hexdigest()

class ComputeClient:
    def __init__(self,descriptor,*,stop=None):
        self.descriptor=dict(descriptor);self.stop=stop;self.metrics=[];self.failed=False
        self.call_lock=threading.Lock();self.connection=None;self.last_call=0.;self.cached_frame=None;self.cached_frame_id=None
        d=self.descriptor
        if (d.get('host')!='127.0.0.1' or type(d.get('port')) is not int or not 1<=d['port']<=65535
                or not re.fullmatch('[0-9a-f]{32}',d.get('job_id',''))
                or not re.fullmatch('[0-9a-f]{64}',d.get('token','')) or d.get('contract')!=contract()):
            raise ValueError('PC 연산 세션·판정 기준이 일치하지 않습니다.')
    def close(self):
        connection,self.connection=self.connection,None
        self.cached_frame=None;self.cached_frame_id=None
        if connection is not None:connection.close()
    def __del__(self):
        try:self.close()
        except Exception:pass
    def call(self,method,frame=None,*,timeout=8.,**args):
        began=time.monotonic()
        while True:
            if self.failed:raise RuntimeError('PC 연산 연결 실패 · 자동 재시도/대체 없음')
            if self.stop is not None and self.stop.is_set():raise RuntimeError('PC 연산 취소됨')
            remaining=timeout-(time.monotonic()-began)
            if remaining<=0:
                self.failed=True;raise TimeoutError('PC 연산 요청 대기 시간 초과')
            if self.call_lock.acquire(timeout=min(.05,remaining)):break
        try:return self._call(method,frame,timeout=timeout,began=began,**args)
        finally:self.call_lock.release()
    def _exchange(self,connection,body,headers,deadline):
        # Socket timeouts alone restart for each received chunk. Interrupt the
        # exact in-flight socket on the cumulative deadline or cancellation.
        finished=threading.Event();interrupted=[None];active_socket=[connection.sock]
        response=None
        def watch():
            while not finished.is_set():
                reason=('cancel' if self.stop is not None and self.stop.is_set() else
                        'failed' if self.failed else 'timeout' if time.monotonic()>=deadline else None)
                if reason:
                    interrupted[0]=reason
                    stream=active_socket[0] or connection.sock
                    if stream is not None:
                        try:stream.shutdown(socket.SHUT_RDWR)
                        except OSError:pass
                        return
                    # Cancellation can win before HTTPConnection assigns its
                    # newly connected socket. Keep watching until it is owned.
                    finished.wait(.005)
                    continue
                finished.wait(min(.05,max(0.,deadline-time.monotonic())))
        def failure():
            if interrupted[0]=='timeout':return TimeoutError('PC 연산 요청 전체 제한시간 초과')
            if interrupted[0]=='cancel':return RuntimeError('PC 연산 취소됨 · 지연 응답 폐기')
            return RuntimeError('PC 연산 실패 이후 도착한 응답 폐기')
        watcher=threading.Thread(target=watch,daemon=True,name='pc-compute-deadline');watcher.start()
        try:
            connection.request('POST','/compute',body,headers)
            # HTTP/1.0 can clear connection.sock while its response still owns
            # the reader; retain the socket before parsing response headers.
            active_socket[0]=connection.sock
            response=connection.getresponse();raw=response.read(MAX_BYTES+1)
            if interrupted[0]:raise failure()
            return response.status,raw
        except Exception as exc:
            if interrupted[0]:raise failure() from exc
            raise
        finally:
            finished.set();watcher.join()
            if response is not None:response.close()
    def _call(self,method,frame,*,timeout,began,**args):
        if self.failed:raise RuntimeError('PC 연산 연결 실패 · 자동 재시도/대체 없음')
        if self.stop is not None and self.stop.is_set():raise RuntimeError('PC 연산 취소됨')
        try:
            d=self.descriptor
            # Reopen before an idle connection could hit the server's 10 s read
            # timeout. A failed request is never retried on another connection.
            if time.monotonic()-self.last_call>2:self.close()
            if frame is not None and (not isinstance(frame,np.ndarray) or frame.nbytes>MAX_BYTES):raise ValueError('Invalid compute frame size')
            capture=None if frame is None else frame.copy()
            reuse=(capture is not None and self.cached_frame is not None and capture.dtype==self.cached_frame.dtype
                   and capture.shape==self.cached_frame.shape and np.array_equal(capture,self.cached_frame))
            fid=self.cached_frame_id if reuse else frame_id(capture)
            encoded_args=encode(args)
            key={'request_id':uuid.uuid4().hex,'job_id':d['job_id'],'contract':d['contract'],'frame_id':fid,
                 'criteria_id':hashlib.sha256(encoded_dumps(encoded_args)).hexdigest()}
            body=encoded_dumps({**key,'method':method,'args':encoded_args,'frame':None if reuse else encode(capture),'frame_reference':reuse})
            remaining=timeout-(time.monotonic()-began)
            if remaining<=0:raise TimeoutError('PC 연산 요청 준비 시간 초과')
            if self.connection is None:self.connection=http.client.HTTPConnection(d['host'],d['port'],timeout=remaining)
            connection=self.connection;connection.timeout=remaining
            if connection.sock is not None:connection.sock.settimeout(remaining)
            status,raw=self._exchange(connection,body,{'Authorization':'Bearer '+d['token'],'Content-Type':'application/json'},began+timeout)
            if status!=200:raise RuntimeError('PC compute rejected: HTTP '+str(status))
            result=loads(raw);elapsed=time.monotonic()-began
            if elapsed>timeout or any(result.get(k)!=v for k,v in key.items()):raise ValueError('PC 연산 응답 시간·프레임·업무·판정 기준 불일치')
            if self.failed:raise RuntimeError('PC 연산 실패 이후 도착한 응답 폐기')
            if self.stop is not None and self.stop.is_set():raise RuntimeError('PC 연산 취소됨 · 지연 응답 폐기')
            if result.get('error'):raise RuntimeError('PC 연산 실패: '+result['error'])
            if frame is not None and result.get('frame_cached')==fid:
                if not reuse:self.cached_frame=capture;self.cached_frame_id=fid
            elif frame is not None:self.cached_frame=None;self.cached_frame_id=None
            self.last_call=time.monotonic()
            self.metrics.append({'method':method,'request_id':key['request_id'],'frame_id':key['frame_id'],
                'roundtrip_seconds':elapsed,'request_bytes':len(body),'response_bytes':len(raw),'frame_reused':reuse,**result.get('timing',{})})
            self.metrics=self.metrics[-2000:]
            return result['value']
        except Exception:
            self.failed=True;self.close();raise
    def provider(self,**kwargs):return RemoteProvider(self,**kwargs)
    def rim(self,frame,mesh,roi,profile,**kwargs):return self.call('rim',frame,mesh=mesh,roi=roi,profile=profile,**kwargs)
    def mask_pose(self,frame,mesh,roi,profile,detections,settings):
        return self.call('mask_pose',frame,mesh=mesh,roi=roi,profile=profile,detections=detections,settings=settings)

class RemoteProvider:
    def __init__(self,client,*,class_ids=(0,1)):
        self.client=client;self.class_ids=tuple(class_ids);self.warmed=False
    def close(self):self.warmed=False
    def prepare(self,frame,settings):
        if self.warmed:return False
        self.client.call('prepare',frame,settings=settings);self.warmed=True;return True
    def predict(self,frame,settings):
        result=self.client.call('predict',frame,settings=settings,class_ids=list(self.class_ids));self.warmed=True;return result

class RemoteInspector:
    supports_evidence=True
    def __init__(self,client):
        self.client=client;self.provider=RemoteProvider(client,class_ids=tuple(range(13)));self.stop=None
    def close(self):self.provider.close()
    def prepare_geometry(self,anchors,profile,shape):
        return self.client.call('prepare_geometry',anchors=anchors,profile=profile,shape=shape)
    def predict_all(self,frame,settings):return self.provider.predict(frame,settings)
    def inspect(self,frame,anchors,profile,*,inference=None,context_anchors=None,evidence=None):
        if self.stop is not None and self.stop.is_set():return []
        wire=inference.to_wire() if inference is not None and inference.rows is not None else None
        result=self.client.call('inspect',frame,anchors=anchors,profile=profile,inference=wire,context_anchors=context_anchors,
                                **({'include_evidence':True} if evidence is not None else {}))
        if evidence is not None:
            from .decision_evidence import notify
            notify(evidence,frame,result['detections'],result['rows']);result=result['rows']
        if self.stop is not None and self.stop.is_set():return []
        return result


def validate_profile(profile):
    from .model_regions import checked_settings
    from .ai_inspection import inspection_settings
    for settings in (checked_settings(profile),inspection_settings(profile)):
        if settings['method']=='model_roi' and settings['model']!='n':
            raise ValueError('PC 협력은 검증된 동일 YOLO26n 모델만 지원합니다.')


def session_for(job_id,command):
    if command.upper() not in PC_COMMANDS or not isinstance(job_id,str) or not re.fullmatch('[0-9a-f]{32}',job_id):
        raise ValueError('PC 연산은 관리되는 PC_BUILD_LOAD 업무 ID가 필요합니다.')
    path=ROOT/'data/pc-compute-sessions'/f'{job_id}.json'
    value=json.loads(path.read_text())
    if value.get('command')!=command.upper() or value.get('expires_at',0)<time.time():raise ValueError('PC 연산 세션이 만료되었거나 명령이 다릅니다.')
    client=ComputeClient(value['descriptor']);client.call('hello')
    return client
