"""Bounded image packet codec shared by display and model-guided detection."""
import json,struct,math
MAX_IMAGE_BYTES=32*1024*1024
DEFAULT_CONFIDENCE=.5

def confidence_value(value):
    """Wire/profile confidence is a finite fraction; the UI displays percent."""
    try:
        if isinstance(value,bool):raise ValueError
        number=float(value)
        if not math.isfinite(number) or not .5<=number<=.9:raise ValueError
        if not math.isclose(number*100,round(number*100),rel_tol=0,abs_tol=1e-9):raise ValueError
    except (TypeError,ValueError,OverflowError):
        raise ValueError('최소 신뢰도는 50~90 사이의 정수(%)로 입력하세요.') from None
    return number

def confidence_percent(value):
    try:
        text=str(value).strip()
        if not text.isascii() or not text.isdecimal():raise ValueError
        return confidence_value(int(text)/100)
    except (TypeError,ValueError,OverflowError):
        raise ValueError('최소 신뢰도는 50~90 사이의 정수(%)로 입력하세요.') from None

def encode_mask(mask):
    """Lossless instance mask; the enclosing packet keeps the same 1 MiB cap."""
    import base64,zlib,numpy as np
    if mask.dtype!=np.bool_ or mask.ndim!=2 or not 0<mask.size<=MAX_IMAGE_BYTES:raise ValueError('잘못된 지그 마스크 형식')
    return base64.b64encode(zlib.compress(np.packbits(mask).tobytes(),1)).decode('ascii')

def decode_mask(value,shape):
    """Bound decompression before allocating a full original-resolution mask."""
    import base64,zlib,numpy as np
    if len(shape)!=2 or any(type(n)!=int or n<=0 for n in shape):raise ValueError('잘못된 지그 마스크 크기')
    pixels=shape[0]*shape[1]
    if pixels>MAX_IMAGE_BYTES or not isinstance(value,str) or len(value)>1024*1024:raise ValueError('지그 마스크가 너무 큽니다.')
    length=(pixels+7)//8
    try:
        data=base64.b64decode(value,validate=True);decoder=zlib.decompressobj();raw=decoder.decompress(data,length+1)
    except (ValueError,zlib.error) as exc:raise ValueError('지그 마스크 해독 실패') from exc
    if len(raw)!=length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:raise ValueError('지그 마스크 데이터 길이가 다릅니다.')
    return np.unpackbits(np.frombuffer(raw,np.uint8),count=pixels).reshape(shape).astype(bool)

def read_exact(stream,length):
    parts=[]
    while length:
        part=stream.read(length)
        if not part:raise EOFError('인식 실행부와의 연결이 종료되었습니다.')
        parts.append(part);length-=len(part)
    return b''.join(parts)

def send_packet(stream,metadata,image=None,codec='raw'):
    import numpy as np
    payload=b'';header=dict(metadata)
    if image is not None:
        if image.dtype!=np.uint8 or image.ndim!=3 or image.shape[2]!=3:raise ValueError('잘못된 카메라 영상 형식')
        if codec=='png':
            import cv2
            ok,encoded=cv2.imencode('.png',image,[cv2.IMWRITE_PNG_COMPRESSION,1])
            if not ok:raise ValueError('인식 영상 전송 인코딩 실패')
            payload=encoded.tobytes()
        elif codec=='raw':payload=np.ascontiguousarray(image).tobytes()
        else:raise ValueError('지원하지 않는 영상 전송 형식')
        header.update(image_codec=codec,image_shape=list(image.shape))
    if len(payload)>MAX_IMAGE_BYTES:raise ValueError('인식 영상이 너무 큽니다.')
    header['image_bytes']=len(payload);raw=json.dumps(header,ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>1024*1024:raise ValueError('인식 응답이 너무 큽니다.')
    for part in (struct.pack('!I',len(raw)),raw,payload):
        view=memoryview(part)
        while view:
            sent=stream.write(view)
            if not sent:raise BrokenPipeError('인식 데이터 전송 실패')
            view=view[sent:]
    stream.flush()

def receive_packet(stream):
    import numpy as np
    length=struct.unpack('!I',read_exact(stream,4))[0]
    if not 0<length<=1024*1024:raise ValueError('잘못된 인식 응답 길이')
    def reject(value):raise ValueError('인식 응답에 유효하지 않은 숫자가 있습니다.')
    result=json.loads(read_exact(stream,length),parse_constant=reject)
    if not isinstance(result,dict):raise ValueError('잘못된 인식 응답 형식')
    size=result.pop('image_bytes',0)
    if type(size)!=int or not 0<=size<=MAX_IMAGE_BYTES:raise ValueError('잘못된 영상 길이')
    if size:
        shape=result.pop('image_shape',None)
        if (not isinstance(shape,list) or len(shape)!=3 or any(type(v)!=int or v<=0 for v in shape)
                or shape[2]!=3 or shape[0]*shape[1]*3>MAX_IMAGE_BYTES):raise ValueError('잘못된 영상 크기')
        payload=read_exact(stream,size);codec=result.pop('image_codec',None)
        if codec=='raw':
            if size!=shape[0]*shape[1]*3:raise ValueError('영상 크기와 데이터 길이가 다릅니다.')
            image=np.frombuffer(payload,dtype=np.uint8).reshape(shape).copy()
        elif codec=='png':
            import cv2
            image=cv2.imdecode(np.frombuffer(payload,np.uint8),cv2.IMREAD_COLOR)
            if image is None or list(image.shape)!=shape:raise ValueError('인식 영상 해독 실패')
        else:raise ValueError('지원하지 않는 영상 응답 형식')
        result['image']=image
    return result

