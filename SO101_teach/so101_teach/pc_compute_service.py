"""Ephemeral PC process: GPU inference and unchanged pure vision operations only."""
import argparse
import hashlib
import re
from copy import deepcopy
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from .pc_compute import MAX_BYTES,contract,dumps,loads,frame_id,decode,encoded_dumps

class GpuProvider:
    def __init__(self,model,reference):
        began=time.monotonic()
        import cv2,numpy,scipy
        actual=(cv2.__version__,numpy.__version__,scipy.__version__)
        if actual!=('4.12.0','2.2.6','1.15.3'):
            raise ValueError('PC geometry runtime must match the validated Pi environment: '+str(actual))
        from .pc_compute_backend import GpuSegmenter
        from .model_regions import runtime,identity
        _,selected,manifest=runtime('n')
        if selected.resolve()!=Path(reference).resolve():raise ValueError('Selected NCNN reference mismatch')
        self.identity=identity(manifest);self.model=GpuSegmenter(model,reference);self.lock=threading.Lock();self.startup_seconds=time.monotonic()-began
    def close(self):pass
    def prepare(self,frame,settings):self.predict(frame,settings);return True
    def predict(self,frame,settings):
        from .model_regions import checked_settings
        settings=checked_settings({'jig_detection':settings})
        if settings['method']!='model_roi' or settings['model']!='n' or settings['model_identity']!=self.identity:
            raise ValueError('PC GPU source model/settings mismatch')
        with self.lock:
            self.model.conf=settings['confidence'];raw=self.model.infer(frame)
            from .yolo_inference import CLASS_LABELS
            rows=[{'class_id':int(cls),'class_name':self.model.names[int(cls)],'label':CLASS_LABELS[int(cls)],'mask_pixels':int(mask.sum()),'confidence':float(score),'box':box.tolist(),'mask':mask}
                for cls,score,box,mask in zip(raw['classes'],raw['scores'],raw['boxes'],raw['masks'])]
            return rows

class ComputeEngine:
    def __init__(self,provider):
        from .ai_inspection import ModelInspector
        self.provider=provider;self.inspector=ModelInspector(provider=provider);self.prepared=None;self.prepared_source=None;self.prepared_key=None
    def detection_frame(self,frame,profile):
        from .lens_geometry import DetectionFrame
        # Every input except the independently supplied support height remains
        # part of the exact key. Never carry a lens/evidence cache to a new frame.
        key=dumps({k:v for k,v in (profile or {}).items() if k!='table_z_mm'})
        if self.prepared_source is not frame or self.prepared_key!=key:
            self.prepared=DetectionFrame(frame,profile);self.prepared_source=frame;self.prepared_key=key
        return self.prepared
    def close(self):self.inspector.close()
    def execute(self,method,frame,args):
        from .preparation_profile import PreparationProfile
        timing=PreparationProfile();self.inspector.timing=timing
        try:return timing.call('pc.'+method,self.dispatch,method,frame,args)
        finally:self.last_timing=timing.snapshot()['metrics']
    def dispatch(self,method,frame,args):
        if method=='hello':return {'ready':True,'backend':'GPU','contract':contract()}
        if method=='prepare':return self.provider.prepare(frame,args['settings'])
        if method=='predict':
            classes=args['class_ids']
            if not classes or any(type(c) is not int or c not in range(13) for c in classes):raise ValueError('Invalid requested classes')
            return [r for r in self.provider.predict(frame,args['settings']) if r['class_id'] in classes]
        if method=='rim':
            from .vision import detect
            return detect(frame,**args,prepared=self.detection_frame(frame,args.get('profile')))
        if method=='mask_pose':
            from .mask_pose import detect_mask_seeded
            return detect_mask_seeded(frame,**args,prepared=self.detection_frame(frame,args.get('profile')))
        if method=='prepare_geometry':return self.inspector.prepare_geometry(**args)
        if method=='inspect':
            from .shared_inference import FrameInference
            from .ai_inspection import inspection_settings
            args=dict(args);wire=args.pop('inference',None);include=args.pop('include_evidence',False)
            if type(include) is not bool:raise ValueError('Invalid evidence flag')
            evidence=FrameInference.from_wire(frame,inspection_settings(args['profile']),wire)
            if wire is not None and evidence is None:raise ValueError('Inspection frame evidence mismatch')
            if not include:return self.inspector.inspect(frame,**args,inference=evidence)
            captured=[]
            rows=self.inspector.inspect(frame,**args,inference=evidence,
                evidence=lambda image,masks,results:captured.extend(masks))
            return {'rows':rows,'detections':captured}
        raise ValueError('Unsupported compute operation')

class ComputeServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,engine,job_id,token,port=0):
        self.engine=engine;self.job_id=job_id;self.token=token;self.contract=contract();self.compute_lock=threading.Lock()
        super().__init__(('127.0.0.1',port),Handler)
    def descriptor(self):return {'host':'127.0.0.1','port':self.server_port,'job_id':self.job_id,'token':self.token,'contract':self.contract}

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    cached_frame=None
    cached_frame_id=None
    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
    def log_message(self,*args):pass
    def do_POST(self):
        self.connection.settimeout(10)
        if self.path!='/compute' or not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token):
            self.send_error(403);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=MAX_BYTES:raise ValueError('Invalid packet size')
            body=self.rfile.read(size)
            if len(body)!=size:raise ValueError('Truncated packet')
            wire=json.loads(body)
            if not isinstance(wire,dict):raise ValueError('Invalid compute request')
            if 'frame_reference' in wire and type(wire['frame_reference']) is not bool:raise ValueError('Invalid frame reference flag')
            criteria=hashlib.sha256(encoded_dumps(wire.get('args'))).hexdigest()
            retained=self.cached_frame.nbytes if wire.get('frame_reference') and self.cached_frame is not None else 0
            value=decode(wire,budget=[MAX_BYTES-retained])
            if (value.get('job_id')!=self.server.job_id or value.get('contract')!=self.server.contract
                    or not re.fullmatch('[0-9a-f]{32}',value.get('request_id',''))
                    or not isinstance(value.get('args'),dict)
                    or value.get('criteria_id')!=criteria):raise ValueError('Compute contract/frame mismatch')
            if value.get('frame_reference',False):
                if (value.get('frame_reference') is not True or value.get('frame') is not None or self.cached_frame is None
                        or value.get('frame_id')!=self.cached_frame_id):raise ValueError('Unknown compute frame reference')
                value['frame']=self.cached_frame
            else:
                if value.get('frame_id')!=frame_id(value.get('frame')):raise ValueError('Compute frame mismatch')
                if value.get('frame') is not None:
                    self.cached_frame=value['frame'];self.cached_frame_id=value['frame_id'];self.cached_frame.flags.writeable=False
            result={k:value[k] for k in ('request_id','job_id','contract','frame_id','criteria_id')}
            if value.get('frame') is not None:result['frame_cached']=self.cached_frame_id
            began=time.monotonic()
            if value['method']=='hello':
                raw=dumps({**result,'value':{'ready':True,'backend':'GPU','contract':self.server.contract},'timing':{'queue_seconds':0.,'compute_seconds':0.}})
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
            if not self.server.compute_lock.acquire(timeout=8):raise TimeoutError('Compute queue expired')
            acquired=time.monotonic()
            try:result['value']=self.server.engine.execute(value['method'],value.get('frame'),value['args'])
            except Exception as exc:result['error']=str(exc)
            finally:
                result['timing']={'queue_seconds':acquired-began,'compute_seconds':time.monotonic()-acquired,'stages':deepcopy(getattr(self.server.engine,'last_timing',{}))}
                self.server.compute_lock.release()
            raw=dumps(result)
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        except (ValueError,KeyError,TypeError,TimeoutError):self.send_error(400)
        except (ConnectionError,OSError):pass

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--model',required=True);parser.add_argument('--reference',required=True);parser.add_argument('--job-id',required=True)
    args=parser.parse_args()
    # Engine startup output belongs on stderr; stdout carries exactly one descriptor.
    output=sys.stdout;sys.stdout=sys.stderr
    engine=ComputeEngine(GpuProvider(args.model,args.reference));server=ComputeServer(engine,args.job_id,secrets.token_hex(32))
    print(json.dumps(server.descriptor()),file=output,flush=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:sys.stdin.buffer.read()
    finally:server.shutdown();server.server_close();engine.close()
if __name__=='__main__':main()
