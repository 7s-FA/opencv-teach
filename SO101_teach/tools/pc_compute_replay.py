"""Replay saved BGR images through Pi NCNN or a job-bound PC compute connection.

Never imports a camera, device link, action server or motion executor.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
import numpy as np
from so101_teach.pc_compute import ComputeClient,dumps,loads,contract,frame_id
from so101_teach.model_regions import RegionModel,identity,runtime
from so101_teach.pc_compute_service import ComputeEngine


def generate(output):
    from so101_teach.inspection_geometry import inspection_context,anchors_for
    from so101_teach.height_reference import support_plane_profile
    _,_,manifest=runtime('n');settings={'method':'model_roi','model':'n','confidence':.5,'model_identity':identity(manifest)}
    output.mkdir(parents=True,exist_ok=False);images=output/'images';images.mkdir();cases=[];sources={}
    def add(label,path,profile,operations):
        frame=cv2.imread(str(path))
        if frame is None:raise ValueError('Cannot decode '+str(path))
        name=label+'.png';cv2.imwrite(str(images/name),frame)
        decoded=cv2.imread(str(images/name))
        assert np.array_equal(frame,decoded)
        sources[label]={'source':str(path.relative_to(ROOT)),'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                        'frame_sha256':frame_id(frame),'png_sha256':hashlib.sha256((images/name).read_bytes()).hexdigest()}
        cases.append({'id':label,'image':'images/'+name,'profile':profile,'operations':operations})
    def selected(profile):return {**deepcopy(profile),'jig_detection':dict(settings),'part_inspection':dict(settings)}
    reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
    folder=ROOT/'tests/fixtures/cad-pallet-current';fixture=json.loads((folder/'fixture.json').read_text())
    for name,case in sorted(fixture['cases'].items()):
        operations=[];profile=None
        for key,jig in case['jigs'].items():
            profile=selected(jig['profile'])
            operations.append({'method':'mask_pose','name':key,'args':{'mesh':jig['mesh'],'roi':jig['roi'],'profile':profile,'settings':settings}})
        add('jig-'+name,folder/(name+'.png'),profile,operations)
    for group in ('part-inspection','neighbor-inspection'):
        folder=ROOT/'tests/fixtures'/group;fixture=json.loads((folder/'fixture.json').read_text());profile=selected(fixture['profile'])
        for path in sorted(folder.glob('*.jpg')):
            frame=cv2.imread(str(path))
            if group=='neighbor-inspection':
                located,context=inspection_context(frame,{},{},{},profile,reference,fixture['placement']);anchors=located['linear'][0]
            else:
                anchors=anchors_for(frame,{},{},{},profile,reference,'리니어 조립','전체',fixture['placement'])[0];context=anchors
            if not anchors:raise ValueError('Missing recorded anchors: '+str(path))
            add(group+'-'+path.stem,path,profile,[{'method':'inspect','name':'linear','args':{'anchors':anchors,'profile':profile,'context_anchors':context,'inference':None}}])
    folder=ROOT/'tests/fixtures/finished-seated-20261011';fixture=json.loads((folder/'fixture.json').read_text());profile=selected(fixture['profile'])
    anchors=deepcopy(fixture['anchors'])
    for anchor in anchors:anchor['camera_from_local']=np.asarray(anchor['camera_from_local'])
    for path in sorted(folder.glob('*.jpg')):
        add('finished-'+path.stem,path,profile,[{'method':'inspect','name':'finished','args':{'anchors':anchors,'profile':profile,'context_anchors':anchors,'inference':None}}])
    # Additional fault images retain their original pixels and geometry. Model selection is explicit.
    folder=ROOT/'tests/fixtures/vision-assembly';fixture=json.loads((folder/'fixture.json').read_text());profile=selected(fixture['profile'])
    for path in sorted(folder.glob('*.png')):
        operations=[]
        for key,jig in fixture['jigs'].items():
            if 'mesh' not in jig:continue
            operations.append({'method':'mask_pose','name':key,'args':{'mesh':jig['mesh'],'roi':jig.get('roi'),'profile':support_plane_profile(profile,jig),'settings':settings}})
        if operations:add('boundary-'+path.stem,path,profile,operations)
    suite={'schema':1,'settings':settings,'contract':contract(),'sources':sources,'cases':cases,
           'scope':'Saved real frames; recorded camera/geometry with current selected YOLO26n checkpoint and current unchanged criteria. No device calls, no episode motion, no actual task total.'}
    (output/'suite.json').write_bytes(dumps(suite))
    print(json.dumps({'output':str(output),'frames':len(cases),'operations':sum(len(c['operations']) for c in cases)},ensure_ascii=False),flush=True)


def rows_signature(value):
    if isinstance(value,list):
        keys=('id','state','quality','seating','product','product_certain','hidden','inspection_allowed','ai_state','ai_product','ai_label','cad_geometric_fault')
        return [{key:row.get(key) for key in keys if key in row} for row in value]
    selected=value.get('selected') or {};metric=selected.get('metric') or {}
    return {'status':value.get('status'),'selected':bool(selected),'verified_for_motion':value.get('verified_for_motion'),
        'cad_pose_validated':selected.get('cad_pose_validated'),'orientation_verified':selected.get('orientation_verified'),
        'symmetry_deg':metric.get('symmetry_deg'),'pallet_pose_method':value.get('pallet_pose_method')}


def replay(suite_path,output,descriptor=None):
    suite=loads(suite_path.read_bytes());folder=suite_path.parent
    if suite['contract']!=contract():raise ValueError('Replay criteria/source contract changed')
    cv2.setNumThreads(1);started=time.monotonic()
    if descriptor:
        client=ComputeClient(json.loads(descriptor.read_text()));client.call('hello');engine=None
        call=lambda method,frame,**args:client.call(method,frame,timeout=30.,**args)
    else:
        engine=ComputeEngine(RegionModel(class_ids=tuple(range(13))));client=None
        call=lambda method,frame,**args:engine.execute(method,frame,args)
    created=time.monotonic();warm_started=time.monotonic()
    call('prepare',np.zeros((480,640,3),np.uint8),settings=suite['settings'])
    warmed=time.monotonic();results=[]
    try:
        for case in suite['cases']:
            frame=cv2.imread(str(folder/case['image']))
            if frame_id(frame)!=suite['sources'][case['id']]['frame_sha256']:raise ValueError('Replay frame changed')
            beginning=time.monotonic();ai_start=beginning
            inspection_only=all(op['method']=='inspect' for op in case['operations'])
            if engine:engine.provider.class_ids=tuple(range(13)) if inspection_only else (0,1)
            detections=[] if inspection_only else call('predict',frame,settings=suite['settings'],class_ids=[0,1])
            ai_seconds=0. if inspection_only else time.monotonic()-ai_start;operations=[]
            for operation in case['operations']:
                args=deepcopy(operation['args']);method=operation['method']
                if method=='mask_pose':args['detections']=detections
                if method=='inspect':args['inference']=None  # Production ModelInspector overlaps AI and CAD.
                before=time.monotonic();value=call(method,frame,**args);elapsed=time.monotonic()-before
                operations.append({'name':operation['name'],'method':method,'seconds':elapsed,'signature':rows_signature(value),'value':value,'stages':engine.last_timing if engine else client.metrics[-1].get('stages',{})})
            if inspection_only:
                ai_seconds=sum(o['stages'].get('inspection.ai',{}).get('total_seconds',0.) for o in operations)
            results.append({'id':case['id'],'frame_sha256':frame_id(frame),'ai_seconds':ai_seconds,'seconds':time.monotonic()-beginning,
                'detections':[{'class_id':r['class_id'],'confidence':r['confidence'],'box':r['box'],'mask_pixels':int(r['mask'].sum())} for r in detections],
                'operations':operations})
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_bytes(dumps({'complete':False,'frames':results}))
            print(json.dumps({'case':case['id'],'seconds':results[-1]['seconds'],'signatures':[o['signature'] for o in operations]},ensure_ascii=False),flush=True)
    finally:
        if engine:engine.close()
    report={'complete':True,'backend':'pc_gpu_over_ssh' if descriptor else 'pi_ncnn','host':platform.node(),'machine':platform.machine(),
        'python':sys.version.split()[0],'opencv':cv2.__version__,'contract':contract(),'model':suite['settings'],
        'startup_seconds':created-started,'warmup_seconds':warmed-warm_started,'frames':results,
        'metrics':client.metrics if client else [],'scope':suite['scope'],'motion_commands':0,'timing_path':'Production ModelInspector AI/CAD concurrency; jig predict plus unchanged per-jig geometry RPCs'}
    output.write_bytes(dumps(report));return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--generate',type=Path);parser.add_argument('--suite',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--descriptor',type=Path)
    args=parser.parse_args()
    if args.generate:generate(args.generate)
    else:replay(args.suite,args.output,args.descriptor)
if __name__=='__main__':main()
