"""Validate installed mask-seeded detection on saved images, without device I/O."""
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import argparse,json,sys,time
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.model_regions import RegionModel,runtime,identity
from so101_teach.vision_service import MultiDetector
from so101_teach.domain import atomic_json

def make_detector(case,settings,provider):
    first=next(iter(case['jigs'].values()));profile=deepcopy(first['profile']);profile['jig_detection']=settings
    items={key:{'roi':row['roi'],'support_height_mm':row['profile']['table_z_mm']-profile['table_z_mm']} for key,row in case['jigs'].items()}
    catalog=SimpleNamespace(items=items,revision=0,mesh=lambda key:deepcopy(case['jigs'][key]['mesh']))
    detector=MultiDetector(catalog,profile,active=next(iter(items)));detector.region_model=provider
    return detector

def observe(detector,frame):
    start=time.perf_counter();result=detector.process(frame);result=detector.finish_observation(result,time.monotonic())
    return result,time.perf_counter()-start

def release(detector):
    detector.region_model=None;detector.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--cases',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--quick',action='store_true');args=p.parse_args()
    cases=json.loads((args.cases/'cases.json').read_text());reference=json.loads(args.reference.read_text());cases=cases[:2] if args.quick else cases
    frames={case['name']:cv2.imread(str(args.cases/case['image'])) for case in cases}
    report={'source':'stored images only','camera_or_motor_io':False,'models':{}}
    for size in ('n','s'):
        settings={'method':'model_roi','model':size,'confidence':.5,'model_identity':identity(runtime(size)[2])}
        provider=RegionModel();rows=[];refs={(r['case'],r['repeat']):r for r in reference['models'][size]['rows']}
        try:
            for case in cases:
                d=make_detector(case,settings,provider)
                try:result,elapsed=observe(d,frames[case['name']])
                finally:release(d)
                assert result['model_inference_called'];expected=refs[(case['name'],0)]['methods']['mask_seed_cv']['jigs'];diffs={}
                for key,raw in result['live_by_jig'].items():
                    wanted=expected[key]['selected'];actual=raw.get('selected')
                    assert bool(wanted)==bool(actual),(size,case['name'],key,raw['status'])
                    if actual:
                        assert raw.get('mask_seed_refined')
                        a,b=wanted['metric'],actual['metric'];center=float(np.linalg.norm(np.array(a['center_xy_mm'])-b['center_xy_mm']));period=a['symmetry_deg'];angle=abs((a['yaw_deg']-b['yaw_deg']+period/2)%period-period/2)
                        assert center<1e-5 and angle<1e-4,(size,case['name'],key,center,angle)
                        diffs[key]={'center_difference_mm':center,'angle_difference_deg':angle}
                    else:assert not raw['candidates'],(size,case['name'],key,'unvalidated candidate escaped')
                rows.append({'case':case['name'],'seconds':elapsed,'selected_jigs':len(diffs),'comparison':diffs})
            timeline=None
            if not args.quick:
                normal=[next(c for c in cases if c['name']=='sequence-'+str(n)) for n in (0,2,5,7,6)];d=make_detector(normal[0],settings,provider);samples=[]
                try:
                    for repeat in range(3):
                        for case in normal:
                            result,elapsed=observe(d,frames[case['name']]);assert all(v['selected'] for v in result['live_by_jig'].values()),(size,case['name'],result['live_by_jig'])
                            samples.append({'case':case['name'],'seconds':elapsed,'model_called':result['model_inference_called'],'states':{key:raw['tracking_state'] for key,raw in result['live_by_jig'].items()}})
                    assert sum(row['model_called'] for row in samples)==1,(size,'unexpected normal inference',samples)
                    end=time.monotonic()+7
                    while not all(v['selected'] for v in result['by_jig'].values()) and time.monotonic()<end:
                        time.sleep(.15);result,_=observe(d,frames[normal[0]['name']])
                    assert all(v['selected'] for v in result['by_jig'].values()),'normal observations never adopted'
                    missing,loss_s=observe(d,np.zeros_like(frames[normal[0]['name']]))
                    assert missing['model_inference_called'] and all(v['selected'] is None for v in missing['by_jig'].values())
                    assert not d.region_tracks
                    wait=max(0,max(d.region_retry_after.values(),default=0)-time.monotonic());time.sleep(wait+.02)
                    recovered,recovery_s=observe(d,frames[normal[0]['name']])
                    assert recovered['model_inference_called'] and all(v['selected'] for v in recovered['live_by_jig'].values())
                    assert all(v['selected'] is None for v in recovered['by_jig'].values()),'recovery bypassed the acquisition window'
                    result=recovered;end=time.monotonic()+7;recovery_observations=1
                    while not all(v['selected'] for v in result['by_jig'].values()) and time.monotonic()<end:
                        time.sleep(.15);result,_=observe(d,frames[normal[0]['name']]);recovery_observations+=1
                        assert not result['model_inference_called']
                    assert all(v['selected'] for v in result['by_jig'].values()),'recovered observations never adopted'
                    timeline={'samples':samples,'normal_model_calls':1,'normal_frames':len(samples),'initial_search_s':samples[0]['seconds'],
                              'normal_tracking_mean_s':float(np.mean([row['seconds'] for row in samples[1:]])),
                              'loss_s':loss_s,'recovery_s':recovery_s,'recovery_wait_s':wait,'recovery_observations':recovery_observations,
                              'old_pose_cleared':True,'new_acquisition_window_required':True,'recovery_adopted':True}
                finally:release(d)
            report['models'][size]={'settings':settings,'cases':rows,'selected_jig_cases':sum(row['selected_jigs'] for row in rows),'timeline':timeline}
            atomic_json(args.output,report);print(size,'PASS',len(rows),'images',sum(row['selected_jigs'] for row in rows),'accepted raw poses',timeline and {'tracking_s':timeline['normal_tracking_mean_s'],'model_calls':1,'recovery_s':timeline['recovery_s']},flush=True)
        finally:provider.close()
    report['status']='passed';atomic_json(args.output,report)
if __name__=='__main__':main()
