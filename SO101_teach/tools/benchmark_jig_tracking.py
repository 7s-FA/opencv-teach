"""Compare complete MultiDetector calls on identical saved images; no hardware I/O."""
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import argparse,hashlib,json,platform,sys,time
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.configuration import JigCatalog
from so101_teach.vision_service import MultiDetector
from so101_teach.model_regions import identity,runtime
from so101_teach.domain import atomic_json


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',type=Path);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3);parser.add_argument('--carrier-only',action='store_true');args=parser.parse_args()
    if args.case:
        folder=args.case;case=json.loads((folder/'case.json').read_text())
    else:
        folder=ROOT/'tests/fixtures/vision-carrier-sequence';config=json.loads((folder/'fixture.json').read_text())
        cat=JigCatalog(ROOT/'data',profile=config['profile']);cat.items=config['jigs']
        for jig in cat.items.values():jig['stl']=str(ROOT/'data/jig_assets'/Path(jig['stl']).name)
        case={'profile':config['profile'],'jigs':cat.items,'meshes':{key:cat.mesh(key) for key in cat.items}}
    keys=[key for key in case['jigs'] if not args.carrier_only or case['meshes'][key].get('shape')=='rectangle']
    names=['0.png','2.png','5.png','7.png','9.png','6.png'];images=[cv2.imread(str(folder/name)) for name in names]
    assert all(im is not None for im in images)
    report={'device':platform.machine(),'frame_size':[images[0].shape[1],images[0].shape[0]],'jigs':keys,'source_frames':{
        name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names},'repeats':args.repeats,
        'scope':'MultiDetector.process + finish_observation; model startup/IPC/ROI/OpenCV/pose latch included; image loading/camera/network/UI excluded',
        'camera_or_motor_io':False,'results':{}}
    for mode in ('opencv','n','s'):
        profile=deepcopy(case['profile']);profile['jig_detection']={'method':'opencv'} if mode=='opencv' else {
            'method':'model_roi','model':mode,'confidence':.5,'model_identity':identity(runtime(mode)[2])}
        catalog=SimpleNamespace(items={key:deepcopy(case['jigs'][key]) for key in keys},revision=0,mesh=lambda key:deepcopy(case['meshes'][key]))
        detector=MultiDetector(catalog,profile,active=keys[0]);rows=[]
        stable=[0,1,2,3,5]  # Frame 9 also loses the pallet in the baseline; test it separately.
        inputs=[('normal',names[i],images[i]) for _ in range(args.repeats) for i in stable]+[('partial_loss',names[4],images[4]),('loss','black',np.zeros_like(images[0])),('recovery',names[0],images[0]),('after_recovery',names[1],images[1])]
        try:
            for phase,name,frame in inputs:
                wait=max(0.,max(detector.region_retry_after.values(),default=0.)-time.monotonic()) if phase=='recovery' else 0.
                if wait:time.sleep(wait+.01)
                started=time.perf_counter();result=detector.process(frame);result=detector.finish_observation(result,time.monotonic());elapsed=time.perf_counter()-started
                live=result['live_by_jig'];rows.append({'phase':phase,'frame':name,'retry_wait_s':wait,'total_s':elapsed,'model_s':result['model_seconds'],'opencv_s':result['opencv_seconds'],
                    'model_called':result['model_inference_called'],'jigs':{key:{'status':raw['status'],'tracking_state':raw.get('tracking_state'),
                        'metric':raw['selected'].get('metric') if raw.get('selected') else None} for key,raw in live.items()}})
                print(mode,phase,name,round(elapsed,4),'model',result['model_inference_called'],{key:row['status'] for key,row in live.items()},flush=True)
            normal=[r for r in rows if r['phase']=='normal'];warm=normal[1:]
            report['results'][mode]={'cold_first_s':normal[0]['total_s'],'warm_mean_s':float(np.mean([r['total_s'] for r in warm])),
                'warm_median_s':float(np.median([r['total_s'] for r in warm])),'warm_p95_s':float(np.percentile([r['total_s'] for r in warm],95)),
                'normal_successes':{key:sum(r['jigs'][key]['metric'] is not None for r in normal) for key in keys},'normal_total_s':sum(r['total_s'] for r in normal),'normal_model_calls':sum(r['model_called'] for r in normal),'rows':rows}
        finally:detector.close()
        atomic_json(args.output,report)
    for mode in ('n','s'):
        delta=[]
        for a,b in zip(report['results']['opencv']['rows'],report['results'][mode]['rows']):
            if a['phase']!='normal':continue
            for key in keys:
                x,y=a['jigs'][key]['metric'],b['jigs'][key]['metric']
                if x and y:delta.append({'key':key,'center_mm':float(np.linalg.norm(np.asarray(x['center_xy_mm'])-y['center_xy_mm'])),
                    'angle_deg':abs((x['yaw_deg']-y['yaw_deg']+90)%180-90)})
        report['results'][mode]['agreement_with_opencv']=delta
    atomic_json(args.output,report)
if __name__=='__main__':main()
