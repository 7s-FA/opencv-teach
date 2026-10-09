"""Isolated kinematic renderer. No device access and no dynamics step."""
import multiprocessing as mp
import queue
import time
from .domain import ROOT,JOINTS
SCENE=ROOT/'assets/so101/preview_scene.xml'
PREVIEW_SIZE=(960,540)
PREVIEW_FPS=30
FRAME_SECONDS=1/PREVIEW_FPS
DEFAULT_LOOKAT=(.15,-.025,.15)
DEFAULT_VIEW=(40.,-25.,1.0,.09,.03,.25)

def overhead_view(profile):
    """Orbit pose with its eye and forward direction at the overhead camera."""
    import math
    from .arm_workspace import preview_profile
    profile=preview_profile(profile)
    matrix=profile.get('extrinsics',{}).get('base_from_camera')
    if matrix is None:return DEFAULT_VIEW
    eye=[float(matrix[i][3])/1000 for i in range(3)]
    forward=[float(matrix[i][2]) for i in range(3)]
    floor=float(profile.get('table_z_mm',-7.4))/1000
    distance=(floor-eye[2])/forward[2] if forward[2]<-1e-6 else .6
    distance=max(.1,distance)
    center=[eye[i]+forward[i]*distance for i in range(3)]
    return (math.degrees(math.atan2(forward[1],forward[0])),math.degrees(math.atan2(forward[2],math.hypot(*forward[:2]))),distance,*center)


def overhead_fovy(profile):
    intrinsics=profile.get('intrinsics')
    if not intrinsics:return 65.
    return _overhead_fovy(tuple(intrinsics['size']),tuple(x for row in intrinsics['K'] for x in row),tuple(intrinsics['D']))

from functools import lru_cache
@lru_cache(maxsize=8)
def _overhead_fovy(size,k,distortion):
    import cv2,numpy as np,math
    matrix,_=cv2.getOptimalNewCameraMatrix(np.array(k).reshape(3,3),np.array(distortion),size,1,size)
    # Retain calibrated coverage at the actual preview aspect ratio.
    half_tangent=max(size[1]/(2*matrix[1,1]),size[0]/(2*matrix[0,0])/(PREVIEW_SIZE[0]/PREVIEW_SIZE[1]))
    return min(110.,max(20.,math.degrees(2*math.atan(half_tangent))))


def pan_view(view,dx,dy,height):
    """Move the scene with the cursor in camera screen axes; world units are metres."""
    import math
    az,el=map(math.radians,view[:2])
    right=(math.sin(az),-math.cos(az),0.)
    up=(-math.cos(az)*math.sin(el),-math.sin(az)*math.sin(el),math.cos(el))
    scale=2*view[2]*math.tan(math.radians(45)/2)/max(1,height)
    origin=view[3:] if len(view)==6 else DEFAULT_LOOKAT
    return (*view[:3],*(p+scale*(-dx*r+dy*u) for p,r,u in zip(origin,right,up)))

def frame_delay_ms(started,now):
    # Account for rendering/GUI work, without catch-up bursts after a slow frame.
    import math
    return max(1,math.ceil(max(0.,FRAME_SECONDS-(now-started))*1000))

def latest(mailbox,item):
    try:mailbox.put_nowait(item);return
    except queue.Full:pass
    try:mailbox.get_nowait()
    except queue.Empty:pass
    try:mailbox.put_nowait(item)
    except queue.Full:pass

def scene_signature(config):
    """Measured joint angles/visibility change frame data, not the compiled scene."""
    import json
    from copy import deepcopy
    workcell=deepcopy(config.get('workcell'))
    if workcell:
        workcell.pop('other_arm_pose_kind',None)
        if workcell.get('linear_stage'):workcell['linear_stage'].pop('startup_state',None)
        for key in ('active_arm_visible','other_arm_visible'):workcell.pop(key,None)
        if workcell.get('other_arm_dynamic'):
            workcell.pop('other_arm_joint_angles_rad',None);workcell.pop('joint_angles_rad',None)
    return json.dumps({'meshes':[{k:v for k,v in j.items() if k!='pose'} for j in config['jigs']],
                       'tcp':config.get('tcp'),'workcell':workcell},sort_keys=True)

def apply_other_arm_pose(model,data,placement):
    if not placement or not placement.get('other_arm_dynamic'):return
    for name,angle in zip(JOINTS,placement.get('other_arm_joint_angles_rad',placement['joint_angles_rad'])):
        data.qpos[model.joint('arm3_preview_'+name).qposadr[0]]=angle

def arm_visibility(model):
    """Remember original opacity once for each compiled display model."""
    import mujoco,numpy as np
    groups={}
    for key,name in (('active','base_link'),('other','arm3_preview')):
        root=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_BODY,name)
        if root<0:continue
        bodies={root}
        for i in range(root+1,model.nbody):
            if model.body_parentid[i] in bodies:bodies.add(i)
        indices=np.array([i for i in range(model.ngeom) if model.geom_bodyid[i] in bodies],dtype=int)
        sites=np.array([i for i in range(model.nsite) if model.site_bodyid[i] in bodies],dtype=int)
        groups[key]=(indices,model.geom_rgba[indices,3].copy(),sites,model.site_rgba[sites,3].copy())
    return groups

def apply_arm_visibility(model,groups,placement):
    placement=placement or {}
    for key,(indices,opacity,sites,site_opacity) in groups.items():
        visible=placement.get(key+'_arm_visible',True)
        model.geom_rgba[indices,3]=opacity if visible else 0.
        model.site_rgba[sites,3]=site_opacity if visible else 0.

def model_data(angles):
    import mujoco
    m=mujoco.MjModel.from_xml_path(str(SCENE));d=mujoco.MjData(m)
    for name,a in zip(JOINTS,angles):d.qpos[m.joint(name).qposadr[0]]=a
    mujoco.mj_forward(m,d)
    return m,d

def contact_metrics(angles):
    import numpy as np
    m,d=model_data(angles);floor=float(d.geom('preview_floor').xpos[2]*1000)
    result={'floor_z_mm':floor,'fingers':{}}
    for name in ('xlerobot_fixed_finger','xlerobot_moving_finger'):
        g=m.geom(name).id;mesh=m.geom_dataid[g];start=m.mesh_vertadr[mesh];count=m.mesh_vertnum[mesh]
        xyz=m.mesh_vert[start:start+count]@d.geom_xmat[g].reshape(3,3).T+d.geom_xpos[g]
        bottom=xyz[np.argmin(xyz[:,2])]*1000
        result['fingers'][name]={'lowest_xyz_mm':bottom.tolist(),'floor_gap_mm':float(bottom[2]-floor)}
    result['tcp_mm']=list(d.body('gripper_frame_link').xpos*1000)
    return result

def worker(commands,frames,stop):
    renderer=None;frames.cancel_join_thread()
    try:
        import mujoco,cv2
        from .preview_materials import apply_preview_materials
        m=mujoco.MjModel.from_xml_path(str(SCENE.with_name('inspection_scene.xml')));d=mujoco.MjData(m)
        apply_preview_materials(m)
        m.vis.global_.offwidth=PREVIEW_SIZE[0];m.vis.global_.offheight=PREVIEW_SIZE[1]
        renderer=mujoco.Renderer(m,height=PREVIEW_SIZE[1],width=PREVIEW_SIZE[0])
        camera=mujoco.MjvCamera();camera.lookat[:]=[.15,-.025,.15]
        options=mujoco.MjvOption();options.geomgroup[3]=0;next_frame_at=0.;compiled_signature=None;highlight_colors={};visibility=arm_visibility(m)
        while not stop.is_set():
            try:token,angles,view,jig,table_z,context,requested_at=commands.get(timeout=.2)
            except queue.Empty:continue
            if stop.wait(max(0.,next_frame_at-time.monotonic())):break
            next_frame_at=time.monotonic()+FRAME_SECONDS
            import math,json
            for geom,color in highlight_colors.items():m.geom_rgba[geom]=color
            highlight_colors={}
            config=jig if isinstance(jig,dict) else None
            if config:
                signature=scene_signature(config)
                if signature!=compiled_signature:
                    renderer.close();m=mujoco.MjModel.from_xml_string(configured_scene(config['jigs'],config.get('tcp'),workcell=config.get('workcell')));d=mujoco.MjData(m)
                    apply_preview_materials(m)
                    m.vis.global_.offwidth=PREVIEW_SIZE[0];m.vis.global_.offheight=PREVIEW_SIZE[1];renderer=mujoco.Renderer(m,height=PREVIEW_SIZE[1],width=PREVIEW_SIZE[0]);compiled_signature=signature;visibility=arm_visibility(m)
                for i,item in enumerate(config['jigs']):
                    index=m.body(f'registered_jig_{i}').mocapid[0];m.geom(f'registered_jig_{i}').rgba[3]=1. if item.get('pose') else 0.
                    if not item.get('pose'):d.mocap_pos[index]=[0,0,-10];continue
                    from .workcell_preview import jig_pose_in_world
                    x,y,yaw=item['pose'];xyz,yaw=jig_pose_in_world(config.get('workcell'),[x,y,item['bottom_z_mm']],yaw)
                    d.mocap_pos[index]=[v/1000 for v in xyz]
                    a=math.radians(yaw)/2;d.mocap_quat[index]=[math.cos(a),0,0,math.sin(a)]
            else:
                body=m.body('observed_pallet');index=body.mocapid[0];m.geom('observed_pallet_visual').rgba[3]=1. if jig else 0.
                if jig:
                    d.mocap_pos[index]=[jig[0]/1000,jig[1]/1000,table_z/1000];a=math.radians(jig[2])/2;d.mocap_quat[index]=[math.cos(a),0,0,math.sin(a)]
                else:d.mocap_pos[index]=[0,0,-10]
            if config and config.get('highlight_joint') in JOINTS:
                body=m.joint(config['highlight_joint']).bodyid[0]
                for geom in range(m.ngeom):
                    if m.geom_bodyid[geom]==body and m.geom_group[geom]!=3:
                        highlight_colors[geom]=m.geom_rgba[geom].copy();m.geom_rgba[geom]=[.08,.72,.70,1.]
            for name,a in zip(JOINTS,angles):d.qpos[m.joint(name).qposadr[0]]=a
            placement=(config or {}).get('workcell')
            apply_other_arm_pose(m,d,placement);apply_arm_visibility(m,visibility,placement)
            if config and config.get('workcell'):
                placement=config['workcell']
                table_z+=placement['base_xyz_mm'][2] if placement.get('active_arm_id')=='arm3' else placement.get('arm2_base_z_mm',0.)
            m.geom('preview_floor').pos[2]=table_z/1000
            if config and (config.get('workcell') or {}).get('board'):
                thickness=config['workcell']['board']['visual_thickness_mm']
                m.body('workcell_board').pos[2]=(table_z-thickness/2)/1000
            if config and (config.get('workcell') or {}).get('linear_stage'):
                m.body('linear_stage').pos[2]=table_z/1000
            mujoco.mj_forward(m,d)
            camera.azimuth,camera.elevation,camera.distance=view[:3]
            camera.lookat[:]=view[3:] if len(view)==6 else DEFAULT_LOOKAT
            # Do not look through the camera's own lens housing in first person.
            import numpy as np
            az,el=map(math.radians,view[:2]);forward=np.array([math.cos(az)*math.cos(el),math.sin(az)*math.cos(el),math.sin(el)])
            eye=camera.lookat-forward*camera.distance
            at_camera=np.linalg.norm(eye-d.site('overhead_optical_center').xpos)<.04
            m.vis.global_.fovy=float((config or {}).get('camera_fovy',65.)) if at_camera else 45.
            for name in ('overhead_pcb','overhead_lens'):m.geom(name).rgba[3]=0. if at_camera else 1.
            renderer.update_scene(d,camera=camera,scene_option=options)
            rgb=draw_base_origin(renderer.render(),renderer.scene,m,d,active_arm_id=(placement or {}).get('active_arm_id','arm2'),placement=placement);ok,jpeg=cv2.imencode('.jpg',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,88])
            if not ok:raise RuntimeError('3D 영상 인코딩 실패')
            latest(frames,('frame',token,jpeg.tobytes(),context,requested_at))
    except Exception as exc:latest(frames,('error',str(exc)))
    finally:
        if renderer is not None:renderer.close()

class Renderer:
    def __init__(self,worker_target=worker):
        ctx=mp.get_context('spawn');self.commands=ctx.Queue(1);self.frames=ctx.Queue(1);self.stop=ctx.Event()
        self.process=ctx.Process(target=worker_target,args=(self.commands,self.frames,self.stop),daemon=True);self.process.start();self.token=0;self.closed=False;self.pending=None;self.in_flight=None
    def submit(self,angles,view,jig=None,table_z=-7.4,*,context=None):
        if self.closed:return False
        self.token+=1
        # Keep one running request and one replaceable latest request, never a backlog.
        self.pending=(self.token,angles,view,jig,table_z,context,time.monotonic())
        self._dispatch()
        return True
    def _dispatch(self):
        if self.closed or self.in_flight is not None or self.pending is None:return
        try:self.commands.put_nowait(self.pending)
        except queue.Full:return
        self.in_flight=self.pending[0];self.pending=None
    def poll(self):
        try:item=self.frames.get_nowait()
        except queue.Empty:
            self._dispatch();return None
        if item[0]=='error' or item[0] in ('frame','inspection_error') and item[1]==self.in_flight:
            self.in_flight=None
        self._dispatch()
        return item
    def close(self):
        if self.closed:return
        self.closed=True;self.stop.set();self.process.join(.5)
        if self.process.is_alive():self.process.terminate();self.process.join(.5)
        for q in (self.commands,self.frames):q.cancel_join_thread();q.close()

def append_registered_jig(assets,world,jig,i):
    """Shared complete jig and mounting assembly for teaching and inspection."""
    import xml.etree.ElementTree as ET
    from pathlib import Path
    from .workcell_scene import white_pla_material
    scale={'mm':.001,'cm':.01,'m':1.}[jig['unit']];name=f'registered_jig_{i}'
    if jig.get('stl'):ET.SubElement(assets,'mesh',name=name,file=str(Path(jig['stl']).resolve()),scale=f'{scale} {scale} {scale}')
    body=ET.SubElement(world,'body',name=name,mocap='true',pos='0 0 -10')
    from .jig_heading import mesh_yaw_offset
    if mesh_yaw_offset(jig):
        # Rotate the centered visual under the semantic pose body. An old
        # saved reference without this field keeps its original mesh frame.
        body=ET.SubElement(body,'body',name=name+'_mesh_basis',quat='0 0 0 1')
    x,y,z=jig['low_mm'];sx,sy,_=jig['size_mm'];offset=[-(x+sx/2)/1000,-(y+sy/2)/1000,-z/1000]
    ET.SubElement(body,'site',name=name+'_center',type='sphere',
                  pos=f'0 0 {jig["size_mm"][2]/2000}',size='.004',rgba='0 0 0 0')
    if jig.get('stl'):ET.SubElement(body,'geom',name=name,type='mesh',mesh=name,pos=' '.join(map(str,offset)),material=white_pla_material(assets),contype='0',conaffinity='0',mass='0')
    else:
        points=[[-sx/2000,-sy/2000],[sx/2000,-sy/2000],[sx/2000,sy/2000],[-sx/2000,sy/2000]];z=jig['size_mm'][2]/1000
        for k in range(4):ET.SubElement(body,'geom',name=name if k==0 else name+f'_edge{k}',type='capsule',fromto=' '.join(map(str,[*points[k],z,*points[(k+1)%4],z])),size='.0008',rgba='.1 .65 .65 1',contype='0',conaffinity='0',mass='0')
    from .workcell_scene import append_carrier_platform
    append_carrier_platform(assets,body,jig,i)

def configured_scene(jigs,tcp=None,*,workcell=None):
    """Build visual-only jig bodies from their actual registered STLs."""
    import xml.etree.ElementTree as ET
    from pathlib import Path
    base=SCENE.parent;scene=ET.parse(base/'inspection_scene.xml').getroot();scene.remove(scene.find('include'))
    arm=ET.parse(base/'so101_modified.xml').getroot()
    for child in arm:
        target=scene.find(child.tag)
        if target is not None and child.tag in ('asset','worldbody','actuator','sensor','contact','equality','default'):
            for item in list(child):target.append(item)
        else:scene.append(child)
    for node in scene.iter():
        if node.get('file'):node.set('file',str((base/node.get('file')).resolve()))
    scene.find(".//geom[@name='observed_pallet_visual']").set('rgba','.7 .9 .9 0')
    assets=scene.find('asset');world=scene.find('worldbody')
    for i,jig in enumerate(jigs):append_registered_jig(assets,world,jig,i)
    from .workcell_preview import append_static_arm,place_active_arm
    append_static_arm(scene,workcell)
    place_active_arm(scene,workcell)
    from .workcell_scene import append_environment
    append_environment(scene,workcell)
    robot_plastic='0.23479508 0.26006748 0.28277929 1'
    ET.SubElement(assets,'material',name='robot_and_camera_mount_plastic',rgba=robot_plastic,specular='.12',shininess='.08',reflectance='0')
    mount_names={'overhead_arm_base','overhead_cam_mount_bottom','overhead_cam_mount_middle','overhead_cam_mount_top'}
    for geom in scene.iter('geom'):
        if geom.get('rgba')==robot_plastic or geom.get('name') in mount_names:
            geom.attrib.pop('rgba',None);geom.set('material','robot_and_camera_mount_plastic')
    base_body=scene.find(".//body[@name='base_link']")
    ET.SubElement(base_body,'site',name='base_origin',pos='0 0 0',size='.0025',rgba='.1 .4 .8 1')
    if tcp:
        body=scene.find(".//body[@name='gripper_frame_link']")
        if body is not None:ET.SubElement(body,'site',name='active_tcp',pos=' '.join(str(v/1000) for v in tcp['xyz_mm']),size='.0025',rgba='1 .35 .1 1')
    return ET.tostring(scene,encoding='unicode')


def reference_points(model,data):
    """World positions for drawing only; base origin is not the motor shaft or table."""
    points={'base':data.body('base_link').xpos.copy()}
    import mujoco
    if mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_SITE,'active_tcp')>=0:
        points['tcp']=data.site('active_tcp').xpos.copy()
    if mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_SITE,'arm3_preview_tcp')>=0:
        points['arm3_tcp']=data.site('arm3_preview_tcp').xpos.copy()
    return points


def center_markers(model,data):
    """Only include centers of visible, positioned jigs; hidden observations stay hidden."""
    import mujoco
    points={}
    for i in range(model.nsite):
        name=model.site(i).name
        if name.startswith('linear_pallet_center_'):points[name]=data.site(i).xpos.copy()
        elif name.startswith('registered_jig_') and name.endswith('_center'):
            geom=mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_GEOM,name[:-7])
            if geom>=0 and model.geom_rgba[geom,3]>0 and data.site(i).xpos[2]>-1:
                points[name]=data.site(i).xpos.copy()
    return points


def draw_base_origin(rgb,scene,model,data,*,active_arm_id='arm2',placement=None):
    """Draw a small shaded sphere at the exact projected origin, through the base mesh."""
    import cv2
    from .inspection import project_markers
    import mujoco
    points={'base':data.body('base_link').xpos}
    if mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_BODY,'arm3_preview')>=0:
        points['arm3']=data.body('arm3_preview').xpos
        points.update({k:v for k,v in reference_points(model,data).items() if k!='base'})
    placement=placement or {}
    if not placement.get('active_arm_visible',True):points.pop('base',None);points.pop('tcp',None)
    if not placement.get('other_arm_visible',True):points.pop('arm3',None);points.pop('arm3_tcp',None)
    points.update(center_markers(model,data))
    result=rgb.copy();h,w=result.shape[:2]
    projected=project_markers(scene,points,float(model.vis.global_.fovy),aspect=w/h)
    active='3' if active_arm_id=='arm3' else '2';other='2' if active=='3' else '3'
    occupied=[]
    for name,point in projected.items():
        if name in ('base','arm3','tcp','arm3_tcp') and point is not None:
            x,y=round(point[0]*w),round(point[1]*h);occupied.append((x+6,y-23,x+87,y-2))
    def center_label(label,color,x,y,left=False,below=False):
        (width,height),baseline=cv2.getTextSize(label,cv2.FONT_HERSHEY_SIMPLEX,.45,1)
        preferred=24 if below else -13
        for offset in (preferred,preferred-22,preferred+22,preferred-44,preferred+44,preferred-66,preferred+66):
            tx=max(3,min(w-width-4,x-width-12 if left else x+10));ty=max(height+4,min(h-baseline-4,y+offset))
            box=(tx-3,ty-height-3,tx+width+3,ty+baseline+3)
            if not any(box[0]<b[2] and box[2]>b[0] and box[1]<b[3] and box[3]>b[1] for b in occupied):break
        occupied.append(box)
        if abs(ty-y)>27:cv2.line(result,(x,y),(tx+width//2,ty+baseline),color,1,cv2.LINE_AA)
        cv2.putText(result,label,(tx,ty),cv2.FONT_HERSHEY_SIMPLEX,.45,color,1,cv2.LINE_AA)
    for name,point in projected.items():
        if point is None or not all(0<=v<=1 for v in point):continue
        x,y=round(point[0]*w),round(point[1]*h)
        if name=='base':
            for dx,dy,radius,color in [(0,0,5,(18,56,133)),(-1,-1,4,(33,103,204)),(-2,-2,2,(107,170,248)),(-2,-2,1,(212,231,255))]:
                cv2.circle(result,(x+dx,y+dy),radius,color,-1,cv2.LINE_AA)
            if 'arm3' in points:cv2.putText(result,'Arm '+active,(x+9,y-7),cv2.FONT_HERSHEY_SIMPLEX,.45,(18,56,133),1,cv2.LINE_AA)
        elif name=='arm3':
            cv2.circle(result,(x,y),5,(195,104,12),-1,cv2.LINE_AA)
            cv2.putText(result,'Arm '+other,(x+9,y-7),cv2.FONT_HERSHEY_SIMPLEX,.45,(135,64,8),1,cv2.LINE_AA)
        elif name.startswith('linear_pallet_center_') or name.startswith('registered_jig_'):
            label=('Pallet '+name.rsplit('_',1)[1]) if name.startswith('linear_') else ('Jig '+str(int(name.split('_')[2])+1))
            color=(145,79,15)
            cv2.circle(result,(x,y),4,(255,255,255),-1,cv2.LINE_AA)
            cv2.circle(result,(x,y),5,color,2,cv2.LINE_AA)
            center_label(label,color,x,y,left=True)
        else:
            label,color=('TCP '+other,(8,113,133)) if name=='arm3_tcp' else ('TCP '+active,(194,72,18))
            cv2.drawMarker(result,(x,y),color,cv2.MARKER_CROSS,12,2,cv2.LINE_AA)
            cv2.putText(result,label,(x+9,y-7),cv2.FONT_HERSHEY_SIMPLEX,.45,color,1,cv2.LINE_AA)
    return result
