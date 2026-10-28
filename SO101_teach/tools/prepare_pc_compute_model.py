"""Export a separate GPU engine from the currently deployed NCNN source checkpoint."""
import argparse,hashlib,json,shutil,time
from pathlib import Path

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 root=Path(__file__).resolve().parents[1];source_dir=root/'assets/models/yolo26n_mixed_fp16'
 original=json.loads((source_dir/'manifest.json').read_text());source=Path(original['source_weights'])
 if sha(source)!=original['source_weights_sha256']:raise ValueError('Selected source checkpoint changed')
 target=args.output/'model';target.mkdir(parents=True,exist_ok=False)
 checkpoint=target/'model.pt';shutil.copy2(source,checkpoint)
 import torch,ultralytics
 from ultralytics import YOLO
 torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
 if not torch.cuda.is_available():raise RuntimeError('PC CUDA GPU unavailable')
 started=time.monotonic()
 exported=YOLO(str(checkpoint),task='segment').export(format='engine',imgsz=960,batch=1,dynamic=False,
    quantize=16,end2end=False,nms=False,device=0,workspace=2,verbose=False)
 engine=Path(exported)
 if engine!=target/'model.engine':shutil.copy2(engine,target/'model.engine')
 shutil.copy2(source_dir/'metadata.yaml',target/'metadata.yaml')
 manifest={'schema':1,'model':original['model'],'format':'TensorRT FP16','imgsz':960,
  'source_weights':str(source),'source_weights_sha256':sha(source),'reference_ncnn_manifest_sha256':sha(source_dir/'manifest.json'),
  'reference_ncnn_files':original['files'],'postprocessing':'unchanged Segmenter.infer NCNN preprocessing and postprocessing',
  'runtime':{'torch':torch.__version__,'ultralytics':ultralytics.__version__,'gpu':torch.cuda.get_device_name(0)},
  'files':{name:sha(target/name) for name in ('model.engine','metadata.yaml')},'export_seconds':time.monotonic()-started}
 (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 (args.output/'result.json').write_text(json.dumps({'model_dir':str(target),**manifest},ensure_ascii=False,indent=2)+'\n')
 print('PC_COMPUTE_ENGINE_READY',str(target),flush=True)
if __name__=='__main__':main()
