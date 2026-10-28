"""Current model masks support pallet CAD fitting and carrier OpenCV fitting."""
import atexit,hashlib,json,math,os,select,subprocess,tempfile,time
from pathlib import Path
import cv2
import numpy as np
from .domain import ROOT
from .inference_protocol import send_packet,receive_packet,decode_mask,MAX_IMAGE_BYTES,DEFAULT_CONFIDENCE,confidence_value

METHODS={'기존 OpenCV':'opencv','AI 마스크 + 지그별 정합':'model_roi'}
MODELS={'YOLO26n · FP16':'n','YOLO26s · FP16':'s'}

def checked_settings(profile):
    value=profile.get('jig_detection',{})
    if not isinstance(value,dict) or set(value)-{'method','model','confidence','model_identity'}:raise ValueError('지그 검출 설정 형식 오류')
    result={'method':'opencv','model':'n','confidence':DEFAULT_CONFIDENCE,'model_identity':None,**value}
    if result['method'] not in METHODS.values() or result['model'] not in MODELS.values():raise ValueError('지원하지 않는 지그 검출 방식·모델입니다.')
    if type(result['confidence']) not in (int,float):raise ValueError('최소 신뢰도는 50~90 사이의 정수(%)로 입력하세요.')
    result['confidence']=confidence_value(result['confidence'])
    identity=result['model_identity']
    if result['method']=='model_roi' and (not isinstance(identity,str) or len(identity)!=64 or any(c not in '0123456789abcdef' for c in identity)):
        raise ValueError('지그 영역 모델의 검증 정보가 없습니다. 검출 설정을 다시 적용하세요.')
    return result

def identity(manifest):
    payload={'files':manifest['files'],'provider':manifest.get('_provider_sha256',{})}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def runtime(model):
    path=ROOT/'data/jig-model-runtime.json'
    if not path.is_file():raise ValueError('지그 영역 모델 실행 환경이 준비되지 않았습니다.')
    config=json.loads(path.read_text());model_path=Path(config['models'][model])
    if not Path(config['python']).is_file():raise ValueError('지그 영역 모델의 Python 환경이 없습니다.')
    manifest=json.loads((model_path/'manifest.json').read_text())
    if manifest.get('format')!='NCNN FP16':raise ValueError('지그 영역 검출에는 검증된 NCNN FP16 모델이 필요합니다.')
    manifest['_provider_sha256']={name:hashlib.sha256((Path(config['app_dir'])/'so101_teach'/name).read_bytes()).hexdigest()
                                 for name in ('yolo_worker.py','yolo_inference.py','inference_protocol.py')}
    return config,model_path,manifest

def verify_profile_models(profile, *, inspect_parts=False):
    """Validate saved selections and actual weights without starting inference."""
    settings=[checked_settings(profile)]
    if inspect_parts:
        from .ai_inspection import inspection_settings
        settings.append(inspection_settings(profile))
    checked=set()
    for item in settings:
        if item['method']!='model_roi':continue
        key=(item['model'],item['model_identity'])
        if key in checked:continue
        _,path,manifest=runtime(item['model'])
        if identity(manifest)!=item['model_identity']:
            raise ValueError('선택한 지그 모델의 가중치 또는 실행 코드가 다릅니다. 검출 설정을 다시 적용하세요.')
        for name,expected in manifest['files'].items():
            if Path(name).name!=name:raise ValueError('잘못된 모델 파일 이름: '+name)
            digest=hashlib.sha256()
            with (path/name).open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            if digest.hexdigest()!=expected:raise ValueError('모델 파일 해시가 다릅니다: '+name)
        checked.add(key)


class DeadlineStream:
    def __init__(self,stream,deadline):self.stream=stream;self.deadline=deadline
    def read(self,size):
        ready,_,_=select.select([self.stream],[],[],max(0,self.deadline-time.monotonic()))
        if not ready:raise TimeoutError('지그 영역 모델 응답 시간 초과')
        return os.read(self.stream.fileno(),size)
    def write(self,value):
        _,ready,_=select.select([],[self.stream],[],max(0,self.deadline-time.monotonic()))
        if not ready:raise TimeoutError('지그 영역 모델 전송 시간 초과')
        return os.write(self.stream.fileno(),value[:65536])
    def flush(self):pass

class RegionModel:
    def __init__(self,*,class_ids=(0,1)):
        if not class_ids or any(type(c) is not int or not 0<=c<13 for c in class_ids):raise ValueError('지원하지 않는 모델 클래스')
        self.class_ids=tuple(sorted(set(class_ids)))
        self.process=None;self.log=None;self.key=None;self.sequence=0;self.warmed=False;atexit.register(self.close)
    def close(self):
        process=self.process;self.process=None;self.key=None;self.warmed=False
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:process.wait(.3)
                except subprocess.TimeoutExpired:process.kill();process.wait(1)
            for stream in (process.stdin,process.stdout):stream.close()
        if self.log:self.log.close();self.log=None
    def prepare(self,frame,settings):
        """Warm the current worker once; its image is never a pose observation."""
        if self.warmed and self.key==(settings['model'],settings['model_identity']) and self.process is not None and self.process.poll() is None:return False
        self.predict(frame,settings)
        return True
    def predict(self,frame,settings):
        key=(settings['model'],settings['model_identity'])
        try:
            if self.process is None or self.key!=key or self.process.poll() is not None:
                self.close();config,path,manifest=runtime(settings['model'])
                if identity(manifest)!=settings['model_identity']:raise ValueError('선택한 지그 모델의 가중치 또는 실행 코드가 다릅니다. 검출 설정을 다시 적용하세요.')
                self.log=tempfile.TemporaryFile()
                env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'4','YOLO_AUTOINSTALL':'False'}
                env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
                self.process=subprocess.Popen([config['python'],'-u','-m','so101_teach.yolo_worker','--serve','--backend','ncnn','--model',str(path),'--regions'],
                    cwd=config['app_dir'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,bufsize=0)
                ready=receive_packet(DeadlineStream(self.process.stdout,time.monotonic()+20))
                if ready.get('kind')!='ready':raise RuntimeError(ready.get('error','지그 영역 모델 시작 실패'))
                actual={**ready['model'],'_provider_sha256':ready.get('runtime',{}).get('provider_sha256',{})}
                if identity(actual)!=settings['model_identity']:raise ValueError('지그 영역 모델 실행 코드 확인 실패')
                self.key=key
            self.sequence+=1;deadline=time.monotonic()+8
            send_packet(DeadlineStream(self.process.stdin,deadline),{'kind':'infer','frame_at':self.sequence,'confidence':settings['confidence'],'mask_classes':list(self.class_ids)},frame)
            result=receive_packet(DeadlineStream(self.process.stdout,deadline))
            if result.get('kind')!='result':raise RuntimeError(result.get('error','지그 영역 검출 실패'))
            if result.get('frame_at')!=self.sequence or result.get('confidence')!=settings['confidence']:raise ValueError('지그 영역 결과가 요청 사진과 다릅니다.')
            rows=result['detections']
            if result.get('mask_shape')!=list(frame.shape[:2]) or not isinstance(rows,list) or len(rows)>50:
                raise ValueError('지그 마스크가 요청 사진과 다릅니다.')
            if len(rows)*frame.shape[0]*frame.shape[1]>MAX_IMAGE_BYTES:raise ValueError('지그 마스크 응답이 너무 큽니다.')
            for row in rows:
                if type(row.get('class_id')) is not int or row['class_id'] not in self.class_ids:raise ValueError('요청하지 않은 모델 마스크 클래스')
                box=np.asarray(row.get('box'),float);score=row.get('confidence')
                if box.shape!=(4,) or not np.isfinite(box).all() or not isinstance(score,(int,float)) or not math.isfinite(score):
                    raise ValueError('잘못된 지그 마스크 검출 정보')
                row['mask']=decode_mask(row.get('mask'),list(frame.shape[:2]))
            self.warmed=True
            return rows
        except Exception:
            self.close();raise

def search_regions(frame,mesh,detections,settings):
    """Expand model boxes; do not calculate or return a pose from segmentation."""
    h,w=frame.shape[:2];shape=mesh.get('shape','ring')
    if shape not in ('ring','rectangle'):raise ValueError('이 지그 형상에는 영역 모델이 연결되지 않았습니다.')
    target=0 if shape=='rectangle' else 1  # Installed carrier (220x148) or finished pallet (70x70).
    mask=np.zeros((h,w),np.uint8);regions=[]
    for row in detections:
        if row.get('class_id')!=target:continue
        score=row.get('confidence');box=np.asarray(row.get('box'),float)
        if not isinstance(score,(int,float)) or not math.isfinite(score) or score<settings['confidence']:continue
        if box.shape!=(4,) or not np.isfinite(box).all():raise ValueError('잘못된 지그 모델 영역')
        x1,y1,x2,y2=box
        if x2<=x1 or y2<=y1:continue
        margin=max(16,min(64,.12*max(x2-x1,y2-y1)))
        left,top=np.maximum(0,np.floor([x1-margin,y1-margin])).astype(int)
        right,bottom=np.minimum([w,h],np.ceil([x2+margin,y2+margin])).astype(int)
        if right<=left or bottom<=top:continue
        mask[top:bottom,left:right]=255
        regions.append({'box':[int(left),int(top),int(right),int(bottom)],'confidence':float(score),'class_id':target})
    return mask,regions

def tracking_regions(frame,result):
    """Next search window from this frame's verified OpenCV rim, in raw pixels."""
    selected=result.get('selected')
    if not selected:return []
    points=np.asarray(selected.get('outline_px',selected.get('quad')),float)
    if points.ndim!=2 or points.shape[1]!=2 or len(points)<4 or not np.isfinite(points).all():return []
    lo=points.min(axis=0);hi=points.max(axis=0)
    if np.any(hi<=lo):return []
    h,w=frame.shape[:2]
    margin=max(16,min(64,.12*max(hi-lo)))
    left,top=np.maximum(0,np.floor(lo-margin)).astype(int)
    right,bottom=np.minimum([w,h],np.ceil(hi+margin)).astype(int)
    if right<=left or bottom<=top:return []
    return [{'box':[int(left),int(top),int(right),int(bottom)],'source':'opencv_rim'}]

def expanded_tracking_regions(frame,regions):
    """One local context retry before declaring loss (never full-frame fallback)."""
    h,w=frame.shape[:2]
    return [{**region,'box':[max(0,region['box'][0]-16),max(0,region['box'][1]-16),
                             min(w,region['box'][2]+16),min(h,region['box'][3]+16)]} for region in regions]

def regions_mask(frame,regions):
    mask=np.zeros(frame.shape[:2],np.uint8)
    for region in regions:
        left,top,right,bottom=region['box'];mask[top:bottom,left:right]=255
    return mask
