"""Use Unicode-aware file access for OpenCV in the Windows demo."""
from pathlib import Path
import os

def install():
    if os.name!='nt':return
    import cv2,numpy as np
    def imread(filename,flags=cv2.IMREAD_COLOR):
        try:return cv2.imdecode(np.frombuffer(Path(filename).read_bytes(),dtype=np.uint8),flags)
        except OSError:return None
    def imwrite(filename,image,params=None):
        ok,encoded=cv2.imencode(Path(filename).suffix,image,[] if params is None else params)
        if not ok:return False
        try:Path(filename).write_bytes(encoded.tobytes());return True
        except OSError:return False
    cv2.imread=imread;cv2.imwrite=imwrite
