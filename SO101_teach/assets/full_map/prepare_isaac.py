"""Produce a renderer-neutral scene manifest from the same approved MJCF."""
from pathlib import Path
import os,json,sys,hashlib
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE));os.environ.setdefault('MUJOCO_GL','egl')
import build
import mujoco


def prepare():
    build.make_scene(preserve_joints=True)
    m=mujoco.MjModel.from_xml_path(str(HERE/'full-map-articulated.xml'));d=mujoco.MjData(m)
    reference=build.load_placement(build.ROOT/'data')
    for prefix,angles in [('',[0,-.1,-1,.5,.05,.1]),('arm1_',[0,-.1,-1,.5,.05,.1]),('arm3_preview_',reference['joint_angles_rad'])]:
        for joint,q in zip(build.JOINTS,angles):d.qpos[m.joint(prefix+joint).qposadr[0]]=q
    mujoco.mj_forward(m,d)
    arrays={}; meshes=[]
    for i in range(m.nmesh):
        a=int(m.mesh_vertadr[i]);n=int(m.mesh_vertnum[i]);b=int(m.mesh_faceadr[i]);f=int(m.mesh_facenum[i])
        arrays[f'vertices_{i}']=m.mesh_vert[a:a+n].copy();arrays[f'faces_{i}']=m.mesh_face[b:b+f].copy()
        na=int(m.mesh_normaladr[i]);nn=int(m.mesh_normalnum[i])
        arrays[f'normals_{i}']=m.mesh_normal[na:na+nn].copy()
        arrays[f'normal_indices_{i}']=m.mesh_facenormal[b:b+f].copy()
        meshes.append({'id':i,'name':m.mesh(i).name,'vertices':n,'triangles':f})
    bodies=[]
    robot_roots={m.body(n).id:label for n,label in [('base_link','ARM2'),('arm1_base_link','ARM1'),('arm3_preview_base_link','ARM3')]}
    robot_for_body={};rigid_for_body={}
    for i in range(m.nbody):
        parent=int(m.body_parentid[i]);robot=robot_roots.get(i,robot_for_body.get(parent))
        robot_for_body[i]=robot
        rigid_for_body[i]=i if robot and (m.body_jntnum[i] or i in robot_roots) else rigid_for_body.get(parent)
        q=Rotation.from_matrix(d.xmat[i].reshape(3,3)).as_quat()
        bodies.append({'id':i,'name':m.body(i).name,'parent':parent,'robot':robot,'rigid':rigid_for_body[i],
            'position':d.xpos[i].tolist(),'quaternion':q[[3,0,1,2]].tolist(),
            'mass':float(m.body_mass[i]),'inertia':m.body_inertia[i].tolist(),'center_of_mass':m.body_ipos[i].tolist(),
            'principal_axes':m.body_iquat[i].tolist(),'is_root':i in robot_roots,'joint_count':int(m.body_jntnum[i])})
    geoms=[]
    for i in range(m.ngeom):
        if m.geom_group[i]==3:continue
        mat=int(m.geom_matid[i]);rgba=(m.mat_rgba[mat] if mat>=0 else m.geom_rgba[i]).copy()
        if rgba[3]<.01:continue
        q=Rotation.from_matrix(d.geom_xmat[i].reshape(3,3)).as_quat()
        geoms.append({'id':i,'name':m.geom(i).name or f'visual_{i}','body':int(m.geom_bodyid[i]),
            'robot':robot_for_body[int(m.geom_bodyid[i])],'rigid':rigid_for_body[int(m.geom_bodyid[i])],
            'type':int(m.geom_type[i]),'mesh':int(m.geom_dataid[i]),'size':m.geom_size[i].tolist(),
            'position':d.geom_xpos[i].tolist(),'quaternion':q[[3,0,1,2]].tolist(),
            'material':m.material(mat).name if mat>=0 else None,'rgba':rgba.tolist(),
            'cutaway_wall':bool(m.geom_group[i]==2)})
    joints=[]
    for i in range(m.njnt):
        child=int(m.jnt_bodyid[i]);parent=rigid_for_body[int(m.body_parentid[child])]
        joints.append({'name':m.joint(i).name,'robot':robot_for_body[child],'body0':parent,'body1':child,
            'axis_world':d.xaxis[i].tolist(),'anchor_world':d.xanchor[i].tolist(),
            'axis_local':m.jnt_axis[i].tolist(),'range_rad':m.jnt_range[i].tolist(),
            'position_rad':float(d.qpos[m.jnt_qposadr[i]])})
    assert len(joints)==18
    sys.path.insert(0,str(build.ROOT.parent/'SO101_rl'))
    from render_settings import scene_appearance
    appearance,rendering=scene_appearance()
    source_paths={HERE/'full-map-preview.xml',HERE/'full-map-articulated.xml',HERE/'appearance.json',HERE/'layout.json',
                  Path(__file__).resolve(),HERE/'isaac/export_scene.py',
                  build.ROOT.parent/'SO101_rl/render_settings.py',build.ROOT.parent/'SO101_rl/config/rendering.json'}
    for node in ET.parse(HERE/'full-map-articulated.xml').iter():
        if node.get('file'):source_paths.add((HERE/node.get('file')).resolve())
    data={'schema':1,'units':'m','up_axis':'Z','geometry_source':'approved full-map-articulated.xml',
        'appearance':appearance,'rendering':rendering,'layout':json.loads((HERE/'layout.json').read_text()),
        'meshes':meshes,'bodies':bodies,'geoms':geoms,'joints':joints,
        'sources':{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(source_paths)},
        'physics_scope':'Three fixed-base articulations with position drives; contact/grasp/control validation is separate.'}
    folder=HERE/'isaac';folder.mkdir(exist_ok=True)
    np.savez_compressed(folder/'geometry.npz',**arrays)
    (folder/'scene-manifest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    # Check the articulated pose against the exact approved static display.
    static=mujoco.MjModel.from_xml_path(str(HERE/'full-map-preview.xml'));sd=mujoco.MjData(static);mujoco.mj_forward(static,sd)
    errors={}
    for name in ('base_link','gripper_link','arm1_base_link','arm1_gripper_link','arm3_preview_base_link','arm3_preview_gripper_link'):
        errors[name]=float(np.max(np.abs(d.body(name).xpos-sd.body(name).xpos)))
        assert errors[name]<1e-7,(name,errors[name])
    report={'articulations':3,'revolute_joints':18,'matched_static_pose_error_m':errors,'geoms':len(geoms),'physics_steps':0}
    (folder/'preparation-validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':prepare()
