"""Apply the stock mount's opposite arm socket in CAD; no device access."""
from pathlib import Path
import json,hashlib,xml.etree.ElementTree as ET
import numpy as np
root=Path(__file__).resolve().parents[1]
folder=root/'calibration/mount_geometry'
def vertices(name):
 raw=(folder/'source'/name).read_bytes()
 return np.frombuffer(raw[84:],dtype=[('n','<f4',3),('v','<f4',(3,3)),('a','<u2')])['v'].reshape(-1,3).astype(float)
arm=vertices('arm_base.stl');bottom=vertices('cam_mount_bottom.stl')
# Each dovetail overlaps by 10 mm, at raw STL Z=-10..0 and 73.025..83.025.
# Translate the identical arm base to the other socket; no mesh reflection.
pitch=float(np.ptp(arm[:,2])+np.ptp(bottom[:,2])-20.)
old=json.loads((folder/'right_nominal_pose.json').read_text());new=json.loads(json.dumps(old))
new['left_robot']=False;new['mount_side']='left';new['opposite_socket_pitch_mm']=pitch
new['assembly_translations_original_stl']['arm_base'][2]+=pitch
for key in ('robot_base_link_in_assembly','shoulder_pan_joint_origin_in_assembly'):new[key][1]-=pitch
for key in ('camera_board_center_in_base_link','camera_board_center_relative_to_j1_origin','camera_board_center_from_j1_ground','camera_lens_axis_table_intersection_from_j1_ground'):new[key][1]+=pitch
new['notes'].append('2026-10-01: stock 32x32 module and forward/down optical orientation retained; arm base uses opposite socket. CAD estimate, not measured extrinsics.')
path=folder/'left_nominal_pose.json';path.write_text(json.dumps(new,indent=2)+'\n')
reference=root/'assets/reference/camera.json';camera=json.loads(reference.read_text());ex=camera['extrinsics']
if ex.get('mount_side')!='left':ex['base_from_camera'][1][3]+=pitch
ex.update(mount_side='left',mount_socket_pitch_mm=pitch,geometry_source='calibration/mount_geometry/left_nominal_pose.json',geometry_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),verified=False,verified_for_robot_motion=False,note='Stock 32x32 module on left-side mast. Opposite socket CAD translation; optical offset 0 mm and existing image axes retained. Not a measured camera pose.')
reference.write_text(json.dumps(camera,ensure_ascii=False,indent=2)+'\n')
for name in ('preview_scene.xml','inspection_scene.xml'):
 path=root/'assets/so101'/name;tree=ET.parse(path);scene=tree.getroot();world=scene.find('worldbody')
 for part in ('cam_mount_bottom','cam_mount_middle','cam_mount_top'):
  geom=world.find("geom[@name='overhead_"+part+"']")
  translation=np.array(new['assembly_translations_original_stl'][part]);A=np.array(new['assembly_to_forward_left_up']);base=np.array(new['robot_base_link_in_assembly'])
  geom.set('pos',' '.join(str(x) for x in (A@translation-base)/1000))
 body=world.find("body[@name='overhead_camera_pcb']");body.set('pos',' '.join(str(x) for x in np.array(ex['base_from_camera'])[:3,3]/1000))
 ET.indent(tree,space='  ');tree.write(path,encoding='unicode')
print(json.dumps({'socket_pitch_mm':pitch,'camera_base_xyz_mm':np.array(ex['base_from_camera'])[:3,3].tolist(),'mount_side':ex['mount_side']}))
