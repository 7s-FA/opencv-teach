"""One physical capture, lossless peer frames on the same Pi monotonic clock."""
import base64
import fcntl
import hashlib
import json
import os
import time
from urllib.request import Request,urlopen


def source_identity(source):
    source=str(source)
    return os.path.realpath('/dev/video'+source if source.isdecimal() else source)


class SharedCapture:
    def __init__(self,runtime,source,width,height):
        self.runtime=runtime;self.source=source;self.width=width;self.height=height
        self.cap=None;self.captured_at=None;self.last_at=None
        shared=runtime.data_dir.parent if runtime.data_dir.name=='arm3-runtime' else runtime.data_dir
        self.peer=shared/'arm3-runtime' if runtime.robot_id=='arm2' else shared
        key=hashlib.sha256(source_identity(source).encode()).hexdigest()
        self.lock=(shared/('camera-'+key+'.lock')).open('a')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:pass
        else:
            import cv2
            self.cap=cv2.VideoCapture(source,cv2.CAP_V4L2)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,width);self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT,height)
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE,1);self.cap.set(cv2.CAP_PROP_FPS,10)
    def isOpened(self):return self.cap.isOpened() if self.cap is not None else True
    def read(self):
        if self.cap is not None:
            ok,frame=self.cap.read();self.captured_at=time.monotonic();return ok,frame
        import cv2,numpy as np
        info=json.loads((self.peer/'remote-server.json').read_text())
        token=(self.peer/'remote-token').read_text().strip()
        request=Request('http://127.0.0.1:'+str(info['port'])+'/capture',
                        data=json.dumps({'source':str(self.source),'width':self.width,'height':self.height,'after':self.last_at}).encode(),
                        headers={'Authorization':'Bearer '+token})
        with urlopen(request,timeout=.5) as response:reply=json.load(response)
        if not reply.get('ok'):raise RuntimeError(reply.get('error','공유 카메라 확인 실패'))
        value=reply['value'];at=value.get('at')
        if at is None:return False,None
        if not 0<=time.monotonic()-at<1:raise RuntimeError('공유 카메라 영상 지연')
        frame=cv2.imdecode(np.frombuffer(base64.b64decode(value['image'],validate=True),np.uint8),cv2.IMREAD_COLOR)
        if frame is None:raise RuntimeError('공유 카메라 영상 해독 실패')
        self.captured_at=self.last_at=at;return True,frame
    def release(self):
        if self.cap is not None:self.cap.release()
        self.lock.close()


def capture_frame(runtime,body):
    import cv2
    c=runtime.camera
    if (not c or not c.running or source_identity(c.source)!=source_identity(body.get('source',''))
            or c.width!=body.get('width') or c.height!=body.get('height')):
        raise ValueError('공유 카메라 설정·실행 상태 불일치')
    capture=getattr(c,'capture',None)
    if not isinstance(capture,SharedCapture) or capture.cap is None:raise ValueError('실제 촬영 소유자가 아닙니다.')
    preview=c.preview_frame
    if preview is None or preview[1]==body.get('after'):return {}
    frame,at=preview
    if not 0<=time.monotonic()-at<1:raise ValueError('공유 카메라 원본 영상 지연')
    ok,raw=cv2.imencode('.png',frame,[cv2.IMWRITE_PNG_COMPRESSION,1])
    if not ok:raise ValueError('공유 카메라 영상 인코딩 실패')
    return {'image':base64.b64encode(raw).decode(),'at':at}
