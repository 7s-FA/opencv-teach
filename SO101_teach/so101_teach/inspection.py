"""Model-only settings inspection. No robot/device access or dynamics stepping."""
from copy import deepcopy
from pathlib import Path
import math,queue,time,xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from .domain import ROOT
from .configuration import tcp_matrix,model_tcp,tcp_label
from .preview import latest,FRAME_SECONDS

def numbers(values):return ' '.join(str(float(v)) for v in values)

def base_scene():
    root=ET.Element('mujoco',model='settings_model_inspection')
    ET.SubElement(root,'compiler',angle='radian')
    vis=ET.SubElement(root,'visual');ET.SubElement(vis,'global',offwidth='800',offheight='600',fovy='45')
    ET.SubElement(vis,'map',znear='.001');ET.SubElement(vis,'headlight',ambient='.3 .3 .3',diffuse='.45 .45 .45',specular='.1 .1 .1')
    assets=ET.SubElement(root,'asset');world=ET.SubElement(root,'worldbody')
    ET.SubElement(assets,'texture',type='skybox',builtin='flat',rgb1='.9 .93 .96',rgb2='.9 .93 .96',width='64',height='64')
    ET.SubElement(world,'light',pos='.15 -.2 .4',dir='-.2 .1 -1',diffuse='.35 .35 .35')
    return root,assets,world

def read_triangles(path):
    raw=Path(path).read_bytes();count=int.from_bytes(raw[80:84],'little') if len(raw)>=84 else 0
    dtype=np.dtype([('n','<f4',3),('v','<f4',(3,3)),('a','<u2')])
    if count and len(raw)==84+50*count:tri=np.frombuffer(raw[84:],dtype=dtype)['v'].astype(float)
    else:
        import re
        vertices=re.findall(r'\bvertex\s+(\S+)\s+(\S+)\s+(\S+)',raw.decode('ascii'))
        if not vertices or len(vertices)%3:raise ValueError('STL 삼각형 형식을 확인하세요.')
        tri=np.array(vertices,float).reshape(-1,3,3)
    if not np.isfinite(tri).all():raise ValueError('STL 좌표에 유효하지 않은 값이 있습니다.')
    return tri

def binary_stl(tri):
    out=np.zeros(len(tri),dtype=np.dtype([('n','<f4',3),('v','<f4',(3,3)),('a','<u2')]))
    out['v']=tri
    normals=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);length=np.linalg.norm(normals,axis=1)
    good=length>0;normals[good]/=length[good,None];out['n']=normals
    return b'\0'*80+len(tri).to_bytes(4,'little')+out.tobytes()

def build_scene(spec):
    root,assets,world=base_scene();blobs={};markers={};info={}
    if spec['kind']=='tcp':
        original=ET.parse(ROOT/'assets/so101/so101_modified.xml').getroot()
        body=deepcopy(original.find(".//body[@name='gripper_link']"));body.set('pos','0 0 0');body.set('quat','1 0 0 0')
        for parent in body.iter():
            for child in list(parent):
                if child.tag in ('joint','inertial'):parent.remove(child)
        # A fixed illustrative opening, independent of motor ticks and live pose.
        jaw=body.find(".//body[@name='moving_jaw_so101_v1_link']")
        q=np.fromstring(jaw.get('quat'),sep=' ');rot=Rotation.from_quat(q[[1,2,3,0]])*Rotation.from_euler('z',.4)
        q=rot.as_quat();jaw.set('quat',numbers(q[[3,0,1,2]]));world.append(body)
        used={g.get('mesh') for g in body.iter('geom') if g.get('mesh')}
        for mesh in original.find('asset'):
            if mesh.tag=='mesh' and mesh.get('name') in used:
                node=deepcopy(mesh);node.set('file',str((ROOT/'assets/so101'/node.get('file')).resolve()));assets.append(node)
        frame=body.find(".//body[@name='gripper_frame_link']");pos=np.fromstring(frame.get('pos'),sep=' ');q=np.fromstring(frame.get('quat'),sep=' ');R=Rotation.from_quat(q[[1,2,3,0]]).as_matrix()
        T=tcp_matrix(spec['tcp']);tip=pos+R@T[:3,3]/1000
        markers={'origin':pos.tolist(),'tcp':tip.tolist()}
        for axis,vec in zip('XYZ',np.eye(3)):markers[axis]=(pos+R@vec*.008).tolist()
        # Keep the CAD fixed-finger point visible when a manual TCP is chosen.
        cad=pos+R@np.asarray(model_tcp()['xyz_mm'])/1000
        markers['cad_tip']=cad.tolist()
        for name,point,color in [('origin',pos,'.1 .4 .8 1'),('tcp',tip,'1 .35 .08 1')]:
            ET.SubElement(world,'site',name=name,pos=numbers(point),size='.0012',rgba=color)
        info={'kind':'tcp','tcp_label':tcp_label(spec['tcp']),'xyz_mm':spec['tcp']['xyz_mm'],'offset_mm':float(np.linalg.norm(T[:3,3])),
              'whole_view':[115.,-18.,max(.22,float(np.linalg.norm(tip-pos))*3),0.,0.,-.05],
              'detail_view':[115.,-18.,max(.055,float(np.linalg.norm(tip-pos))*3),*((pos+tip)/2).tolist()]}
    else:
        unit={'mm':1.,'cm':10.,'m':1000.}[spec['unit']]
        if spec.get('stl'):
            tri=read_triangles(spec['stl'])*unit;low=tri.min((0,1));size=tri.max((0,1))-low
            if np.any(size<=0):raise ValueError('STL의 가로·세로·높이를 확인하세요.')
            from .vision import stl_profile
            try:rim=stl_profile(spec['stl'],spec['unit'])['rim_z_mm']-low[2]
            except ValueError:rim=None
            tri-=low+[size[0]/2,size[1]/2,0];tri/=1000
            if spec.get('preview_jig'):
                from .preview import append_registered_jig
                append_registered_jig(assets,world,spec['preview_jig'],0)
                world.find("body[@name='registered_jig_0']").set('pos','0 0 0')
            else:
                blobs['selected.stl']=binary_stl(tri)
                ET.SubElement(assets,'mesh',name='selected',file='selected.stl')
                from .workcell_scene import white_pla_material
                ET.SubElement(world,'geom',type='mesh',mesh='selected',material=white_pla_material(assets),contype='0',conaffinity='0')
        else:
            size=np.array([*spec['size_mm'],spec['rim_mm']],float);rim=size[2]
            if not np.isfinite(size).all() or np.any(size<=0):raise ValueError('가로·세로·높이는 양수여야 합니다.')
            x,y,z=size/1000;pts=[[-x/2,-y/2,z],[x/2,-y/2,z],[x/2,y/2,z],[-x/2,y/2,z]]
            for a,b in zip(pts,pts[1:]+pts[:1]):ET.SubElement(world,'geom',type='capsule',fromto=numbers([*a,*b]),size=str(min(x,y)*.008),rgba='.2 .55 .6 1')
        x,y,z=size/1000;plane=0 if rim is None else rim/1000
        markers={'center':[0,0,plane],'X':[x*.62,0,plane],'Y':[0,y*.62,plane]}
        if rim is not None and spec.get('stl'):
            pts=[[-x/2,-y/2,plane],[x/2,-y/2,plane],[x/2,y/2,plane],[-x/2,y/2,plane]]
            for a,b in zip(pts,pts[1:]+pts[:1]):ET.SubElement(world,'geom',type='capsule',fromto=numbers([*a,*b]),size=str(max(x,y)*.002),rgba='.1 .6 .65 1')
        distance=float(max(size)/1000*2.1)
        info={'kind':'jig','size_mm':size.tolist(),'rim_mm':None if rim is None else float(rim),'has_stl':bool(spec.get('stl')),
              'whole_view':[125.,-30.,distance,0.,0.,z/2], 'detail_view':[90.,-89.,distance,0.,0.,z/2]}
        if spec.get('preview_jig'):
            info['main_scene_assembly']=True
            if spec['preview_jig'].get('platform')=='TurtleBot3 Burger':
                height=spec['preview_jig']['platform_height_mm']/1000
                info['whole_view']=[125.,-25.,max(distance,.62),0.,0.,(z-height)/2]
    ET.SubElement(root,'statistic',extent=str(max(.01,info['whole_view'][2]/2)))
    return ET.tostring(root,encoding='unicode'),blobs,markers,info

def project_markers(scene,markers,fovy=45,*,aspect=4/3):
    cams=scene.camera;pos=(np.asarray(cams[0].pos)+np.asarray(cams[1].pos))/2
    forward=np.asarray(cams[0].forward);forward=forward/np.linalg.norm(forward)
    up=np.asarray(cams[0].up);up=up/np.linalg.norm(up);right=np.cross(forward,up);out={}
    for key,xyz in markers.items():
        delta=np.asarray(xyz)-pos;depth=float(delta@forward)
        if depth<=0:continue
        scale=depth*math.tan(math.radians(fovy)/2)
        out[key]=[float(.5+(delta@right)/(scale*2*aspect)),float(.5-(delta@up)/(scale*2))]
    return out

def inspection_worker(commands,frames,stop):
    import mujoco,cv2,json
    renderer=None;signature=None;next_at=0.;frames.cancel_join_thread()
    try:
        while not stop.is_set():
            try:token,_,view,spec,_,context,requested_at=commands.get(timeout=.2)
            except queue.Empty:continue
            if stop.wait(max(0,next_at-time.monotonic())):break
            next_at=time.monotonic()+FRAME_SECONDS
            try:
                key=json.dumps(spec,sort_keys=True)
                if key!=signature:
                    xml,blobs,markers,info=build_scene(spec)
                    model=mujoco.MjModel.from_xml_string(xml,assets=blobs);data=mujoco.MjData(model);mujoco.mj_forward(model,data)
                    if renderer:renderer.close()
                    renderer=mujoco.Renderer(model,height=600,width=800);signature=key
                actual_view=info['detail_view' if view=='detail' else 'whole_view'] if isinstance(view,str) else view
                cam=mujoco.MjvCamera();cam.azimuth,cam.elevation,cam.distance=actual_view[:3];cam.lookat[:]=actual_view[3:]
                renderer.update_scene(data,camera=cam);rgb=renderer.render()
                ok,jpeg=cv2.imencode('.jpg',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,90])
                if not ok:raise RuntimeError('모델 이미지 변환 실패')
                meta={**info,'view':list(actual_view),'markers':project_markers(renderer.scene,markers)}
                latest(frames,('frame',token,jpeg.tobytes(),context,requested_at,meta))
            except Exception as exc:latest(frames,('inspection_error',token,str(exc),context,requested_at))
    finally:
        if renderer:renderer.close()
