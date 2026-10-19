"""Saved-photo model ROI and calibrated OpenCV rim integration, no hardware I/O."""
from pathlib import Path
import sys,json,time,argparse
import cv2,numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.vision import detect
from so101_teach.model_regions import RegionModel,runtime,identity,search_regions
from so101_teach.domain import atomic_json

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--case',type=Path);parser.add_argument('--output',type=Path);args=parser.parse_args()
 folder=ROOT/'tests/fixtures/vision-carrier-sequence'
 if args.case:
  case=json.loads((args.case/'case.json').read_text());profile=case['profile'];jigs=case['jigs'];meshes=case['meshes'];image=cv2.imread(str(args.case/'frame.png'))
 else:
  config=json.loads((folder/'fixture.json').read_text());profile=config['profile']
  catalog=JigCatalog(ROOT/'data',profile=profile);catalog.items=config['jigs']
  for jig in catalog.items.values():jig['stl']=str(ROOT/'data/jig_assets'/Path(jig['stl']).name)
  jigs=catalog.items;meshes={key:catalog.mesh(key) for key in jigs};image=cv2.imread(str(folder/'0.png'))
 out=args.output or ROOT/'verification/model-guided-jigs-20261018';out.mkdir(parents=True,exist_ok=True);report={}
 for size in ('n','s'):
  model=RegionModel();settings={'method':'model_roi','model':size,'confidence':.5,'model_identity':identity(runtime(size)[2])}
  try:
   started=time.monotonic();rows=model.predict(image,settings);entry={'model_detections':rows,'region_elapsed_s':time.monotonic()-started,'jigs':{}}
   for key,jig in jigs.items():
    if key=='pallet':continue
    mesh=meshes[key];plane=support_plane_profile(profile,jig)
    original=detect(image,mesh,jig.get('roi'),plane)
    mask,regions=search_regions(image,mesh,rows,settings)
    assert regions,('Model did not find fixture jig',size,key)
    guided=detect(image,mesh,jig.get('roi'),plane,search_mask=mask)
    assert original['selected'] and guided['selected'],(size,key,original['status'],guided['status'])
    a,b=original['selected']['metric'],guided['selected']['metric']
    center=float(np.linalg.norm(np.array(a['center_xy_mm'])-b['center_xy_mm']));angle=abs((a['yaw_deg']-b['yaw_deg']+90)%180-90)
    assert center<=.3 and angle<=.2,(size,key,center,angle)
    entry['jigs'][key]={'baseline':a,'model_guided':b,'center_difference_mm':center,'angle_difference_deg':angle,'regions':regions}
   assert entry['jigs'];report[size]=entry
  finally:model.close()
 atomic_json(out/'recorded-integration.json',{'status':'passed','source':str(args.case/'frame.png' if args.case else folder/'0.png'),'results':report,'camera_or_motor_io':False})
 print(json.dumps({s:{k:{a:v for a,v in row.items() if a in ('center_difference_mm','angle_difference_deg')} for k,row in report[s]['jigs'].items()} for s in report},ensure_ascii=False))
if __name__=='__main__':main()
