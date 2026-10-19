"""Replace only the SAM3 cached results after verified runtime optimization."""
import hashlib,json,sys,time
from pathlib import Path
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.yolo_worker import YoloWorker
from so101_teach.domain import atomic_json

def wait(worker,kind,seconds=300):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  result=worker.poll()
  if result and result['kind']=='error':raise RuntimeError(result['error'])
  if result and result['kind']==kind:return result
  time.sleep(.02)
 raise TimeoutError(kind)

def main():
 folder=ROOT/'data/model-comparison';index=folder/'index.json';doc=json.loads(index.read_text())
 before={row['id']:{k:v for k,v in row['models'].items() if k!='sam3'} for row in doc['images']}
 config=json.loads((ROOT/'data/yolo-runtime.json').read_text());path=ROOT/'assets/models/sam3_mixed_bf16';manifest=json.loads((path/'manifest.json').read_text())
 worker=YoloWorker();started=time.monotonic();runtime=None
 try:
  worker.start(path,backend='sam3',python=config['sam3_python'],expected_manifest=manifest);ready=wait(worker,'ready');startup=time.monotonic()-started
  assert ready['runtime']['compiled_graphs']>=1 and ready['runtime']['text_cache_size']==13
  for row in doc['images']:
   source=Path(row['source']);assert hashlib.sha256(source.read_bytes()).hexdigest()==row['source_sha256']
   image=cv2.imread(str(source));assert worker.submit(image,time.monotonic(),.5)
   result=wait(worker,'result',30);filename=row['id']+'-sam3-optimized.jpg'
   assert cv2.imwrite(str(folder/filename),result['image'],[cv2.IMWRITE_JPEG_QUALITY,95])
   row['models']['sam3']={k:result[k] for k in ('inference_ms','total_ms','detections','runtime','model')}
   row['models']['sam3'].update(image=filename,label='SAM3 · 최적화')
   assert {k:v for k,v in row['models'].items() if k!='sam3'}==before[row['id']]
   runtime=result['runtime'];print(row['id'],'complete',flush=True)
  doc['sam3_optimization_updated_at']=time.time();atomic_json(index,doc)
  atomic_json(ROOT/'verification/four-models-20261018/sam3-optimization-refresh.json',{'status':'complete','images':len(doc['images']),'other_three_models_unchanged':True,'startup_s':startup,'runtime':runtime})
 finally:worker.close()
if __name__=='__main__':main()
