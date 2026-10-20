"""AI evidence scoped to one captured frame, never a temporal confirmation."""
from copy import deepcopy
import hashlib
import math
import threading


def frame_signature(frame):
    return (frame.shape,frame.dtype.str,hashlib.sha256(frame.tobytes()).digest())


def settings_signature(settings):
    return tuple(settings.get(key) for key in ('method','model','model_identity','confidence'))


class FrameInference:
    """Share all-class masks without sharing mutable results or observation votes."""
    def __init__(self,frame,settings,predict):
        self.frame_signature=frame_signature(frame)
        self.settings_signature=settings_signature(settings)
        self.predict=predict;self.lock=threading.Lock()
        self.rows=None;self.calls=0;self.hits=0
    def get(self,frame,settings):
        # A copied raw frame is valid; a different image, crop, JPEG decode,
        # model identity or threshold must go through a fresh inference.
        if (settings_signature(settings)!=self.settings_signature
                or frame_signature(frame)!=self.frame_signature):
            return None
        with self.lock:
            if self.rows is None:
                rows=self.predict(frame,settings)
                if (frame_signature(frame)!=self.frame_signature
                        or settings_signature(settings)!=self.settings_signature):
                    return None
                # Do not publish partially failed results for the second consumer.
                self.rows=deepcopy(rows);self.calls+=1
            else:self.hits+=1
            return deepcopy(self.rows)
    def to_wire(self):
        from .inference_protocol import encode_mask
        if self.rows is None:raise ValueError('완료되지 않은 AI 관측은 공유할 수 없습니다.')
        shape,dtype,digest=self.frame_signature
        return {'schema':1,'shape':list(shape),'dtype':dtype,'sha256':digest.hex(),
                'settings':list(self.settings_signature),
                'rows':[{**row,'mask':encode_mask(row['mask'])} for row in self.rows]}
    @classmethod
    def from_wire(cls,frame,settings,value):
        if not value:return None
        signature=frame_signature(frame)
        if (not isinstance(value,dict) or value.get('schema')!=1
                or value.get('shape')!=list(signature[0]) or value.get('dtype')!=signature[1]
                or value.get('sha256')!=signature[2].hex()
                or value.get('settings')!=list(settings_signature(settings))):
            return None
        from .inference_protocol import decode_mask,MAX_IMAGE_BYTES
        rows=value.get('rows');pixels=frame.shape[0]*frame.shape[1]
        if not isinstance(rows,list) or len(rows)>50 or len(rows)*pixels>MAX_IMAGE_BYTES:
            raise ValueError('공유 AI 마스크 크기 오류')
        decoded=[]
        for row in rows:
            if (not isinstance(row,dict) or type(row.get('class_id')) is not int
                    or not 0<=row['class_id']<13 or type(row.get('confidence')) not in (int,float)
                    or not math.isfinite(row['confidence'])
                    or not isinstance(row.get('box'),list) or len(row['box'])!=4
                    or any(type(n) not in (int,float) or not math.isfinite(n) for n in row['box'])):
                raise ValueError('공유 AI 관측 형식 오류')
            decoded.append({**row,'mask':decode_mask(row.get('mask'),list(frame.shape[:2]))})
        result=cls(frame,settings,None);result.rows=decoded
        return result


def shared_frame(frame,profile,inspector):
    """Only identical AI settings can serve jig and part consumers together."""
    from .ai_inspection import inspection_settings
    from .model_regions import checked_settings
    jig=checked_settings(profile);part=inspection_settings(profile)
    if jig['method']!='model_roi' or jig!=part or not hasattr(inspector,'predict_all'):
        return None
    return FrameInference(frame,part,inspector.predict_all)
