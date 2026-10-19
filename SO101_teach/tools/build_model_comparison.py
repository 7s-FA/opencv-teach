"""Cache the four real model outputs for every held-out real test photo."""
import hashlib,json,sys,time
from pathlib import Path
import cv2
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.yolo_worker import YoloWorker
from so101_teach.yolo_ui import MODEL_PATHS,MODEL_NAMES
from so101_teach.pi_connection import load,validate
from so101_teach.domain import atomic_json

def wait(worker,kind,seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=worker.poll()
        if result and result['kind']=='error':raise RuntimeError(result['error'])
        if result and result['kind']==kind:return result
        time.sleep(.02)
    raise TimeoutError(kind)

def main():
    source=ROOT.parent/'SO101_synthetic/benchmarks/real_v7_yolo26s_seg_20261016/dataset'
    out=ROOT/'data/model-comparison';out.mkdir(exist_ok=True)
    config=json.loads((ROOT/'data/yolo-runtime.json').read_text())
    photos=[source/'images'/(Path(line).stem+'.png') for line in (source/'test.txt').read_text().splitlines() if line.strip()]
    document={'version':1,'confidence':.5,'source':'held-out real test set','images':[],
              'note':'같은 저장 사진을 각 모델의 실제 실행부로 추론했습니다. 시간은 사진별 1회 측정이며 실시간 FPS가 아닙니다.'}
    for photo in photos:
        frame=cv2.imread(str(photo));assert frame is not None
        original=photo.stem+'-original.jpg';assert cv2.imwrite(str(out/original),frame,[cv2.IMWRITE_JPEG_QUALITY,95])
        document['images'].append({'id':photo.stem,'original':original,'source':str(photo),
                                  'source_sha256':hashlib.sha256(photo.read_bytes()).hexdigest(),'models':{}})
    for key in ('pc','sam3','pi','pi_n'):
        worker=YoloWorker();remote=key in ('pi','pi_n')
        options={'backend':'ncnn' if remote else 'sam3' if key=='sam3' else 'tensorrt',
                 'python':config['pi_python' if remote else 'sam3_python' if key=='sam3' else 'pc_python'],
                 'expected_manifest':json.loads((MODEL_PATHS[key]/'manifest.json').read_text())}
        path=MODEL_PATHS[key]
        if remote:
            path=config['pi_n_model' if key=='pi_n' else 'pi_model']
            options.update(remote=validate(load(ROOT/'data'),True),remote_dir=config['pi_app_dir'])
        try:
            worker.start(path,**options);wait(worker,'ready')
            worker.submit(cv2.imread(str(photos[0])),time.monotonic(),.5);wait(worker,'result')
            for photo,item in zip(photos,document['images']):
                frame=cv2.imread(str(photo))
                while not worker.submit(frame,time.monotonic(),.5):time.sleep(.02)
                result=wait(worker,'result');filename=photo.stem+'-'+key+'.jpg'
                assert cv2.imwrite(str(out/filename),result['image'],[cv2.IMWRITE_JPEG_QUALITY,95])
                item['models'][key]={k:result[k] for k in ('inference_ms','total_ms','detections','runtime','model')}
                item['models'][key].update(image=filename,label=MODEL_NAMES[key])
            print(key,len(photos),'completed',flush=True)
            atomic_json(out/'progress.json',{'completed_model':key,'photos':len(photos),'updated_at':time.time()})
        finally:worker.close()
    document['status']='complete';atomic_json(out/'index.json',document)
    print('COMPLETE',out/'index.json',flush=True)

if __name__=='__main__':main()
