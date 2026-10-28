"""GPU network evaluation with the unchanged NCNN pre/post-processing code."""
from pathlib import Path
from types import SimpleNamespace
import hashlib,json
import numpy as np
from .yolo_inference import Segmenter,checked_manifest,label_font


class GpuSegmenter(Segmenter):
    """Only replace the neural network; Segmenter.infer owns every pixel rule."""
    def __init__(self,path,reference,threads=4,conf=.5,iou=.7):
        import torch,yaml,cv2
        from ultralytics.nn.autobackend import AutoBackend
        self.path=Path(path);reference=Path(reference);self.manifest=checked_manifest(self.path)
        original=checked_manifest(reference)
        if self.manifest.get('format')!='TensorRT FP16':raise ValueError('PC GPU engine must use FP16')
        if (self.manifest.get('source_weights_sha256')!=original.get('source_weights_sha256')
            or self.manifest.get('reference_ncnn_files')!=original['files']):
            raise ValueError('PC GPU and Pi NCNN must use the same selected checkpoint')
        if not torch.cuda.is_available():raise RuntimeError('PC CUDA GPU unavailable; no automatic fallback')
        torch.set_num_threads(threads);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        cv2.setNumThreads(1)
        self.backend=AutoBackend(model=str(self.path/'model.engine'),device=torch.device('cuda:0'),fp16=True,verbose=False,end2end=False)
        metadata=yaml.safe_load((reference/'metadata.yaml').read_text())
        self.size=tuple(metadata['imgsz']);self.names={int(k):v for k,v in metadata['names'].items()}
        self.end2end=metadata.get('end2end',False);self.conf=conf;self.iou=iou;self.font=label_font();self.kind='tensorrt_same_model'
        if self.end2end:raise ValueError('This engine contract expects the selected non-end2end NCNN model')
        owner=self
        class Extractor:
            def __enter__(self):self.values=None;return self
            def __exit__(self,*args):self.values=None
            def input(self,name,x):
                with torch.inference_mode():
                    tensor=torch.from_numpy(np.ascontiguousarray(x)).unsqueeze(0).to(owner.backend.device)
                    tensor=tensor.half() if owner.backend.fp16 else tensor.float()
                    output=owner.backend(tensor)
                    if not isinstance(output,(tuple,list)) or len(output)!=2:raise ValueError('Unexpected GPU segmentation output')
                    self.values=[value.float().cpu().numpy()[0] for value in output]
                    torch.cuda.synchronize()
                return 0
            def extract(self,name):return 0,self.values[int(name[-1])]
        self.ncnn=SimpleNamespace(Mat=lambda x:x)
        self.net=SimpleNamespace(input_names=lambda:['images'],output_names=lambda:['output0','output1'],create_extractor=Extractor)
