"""Display-only workcell placement. Never used by IK, calibration or motor control."""
from copy import deepcopy
import json
import math
from pathlib import Path

WORKCELL_VIEW=(135.,-42.,1.45,.14,.25,.14)


def teaching_placement(placement,profile):
    """Display the selected arm's work endpoint without changing live state."""
    value=deepcopy(placement)
    if not value or not value.get('linear_stage'):return value
    from .arm_workspace import arm_id
    stage=value['linear_stage'];forward=arm_id(profile)=='arm2'
    endpoint=stage.get('endpoint_reference',{}).get('forward' if forward else 'retracted',{})
    target=float(endpoint.get('commanded_mm',100. if forward else 1.5))
    stage['stroke_mm']=target
    stage['startup_state']={'known':True,'commanded_mm':target,'moving':False,'pending':False,'measured':False,'preview_only':True}
    return value


from .workcell_placement import load_placement,base_transform


def placed_tcp_pose(placement):
    from .configuration import model_tcp
    from .geometry import Kinematics
    kin=Kinematics(None,tcp=placement.get('tcp') or model_tcp())
    return base_transform(placement)@kin.fk_angles(placement['joint_angles_rad'])


def append_static_arm(scene,placement):
    """Reuse only the arm subtree; bake its pose and omit joints/actuators/mounts."""
    if placement is None:return
    import xml.etree.ElementTree as ET
    import numpy as np
    from scipy.spatial.transform import Rotation
    from .domain import JOINTS
    source=scene.find(".//body[@name='base_link']")
    arm=deepcopy(source)
    active=placement.get('active_arm_id','arm2')
    dynamic=bool(placement.get('other_arm_dynamic'))
    angles=dict(zip(JOINTS,placement.get('other_arm_joint_angles_rad',placement['joint_angles_rad'])))
    for body in arm.iter('body'):
        joint=body.find('joint')
        if joint is not None and not dynamic:
            q=np.fromstring(body.get('quat','1 0 0 0'),sep=' ')
            rotation=Rotation.from_quat(q[[1,2,3,0]])
            axis=np.fromstring(joint.get('axis','0 0 1'),sep=' ')
            turn=Rotation.from_rotvec(axis/np.linalg.norm(axis)*angles[joint.get('name')])
            anchor=np.fromstring(joint.get('pos','0 0 0'),sep=' ')
            pos=np.fromstring(body.get('pos','0 0 0'),sep=' ')+rotation.apply(anchor-turn.apply(anchor))
            quat=(rotation*turn).as_quat()[[3,0,1,2]]
            body.set('pos',' '.join(map(str,pos)));body.set('quat',' '.join(map(str,quat)))
            body.remove(joint)
        if not dynamic:
            for inertia in list(body.findall('inertial')):body.remove(inertia)
    for node in arm.iter():
        if node.get('name'):node.set('name','arm3_preview_'+node.get('name'))
        if node.tag=='geom':
            node.set('contype','0');node.set('conaffinity','0');node.set('mass','0');node.attrib.pop('density',None)
    yaw=math.radians(placement['base_yaw_deg'] if active=='arm2' else 0.)/2
    root=ET.SubElement(scene.find('worldbody'),'body',name='arm3_preview',
        pos=' '.join(str(v/1000) for v in (placement['base_xyz_mm'] if active=='arm2' else [0,0,placement.get('arm2_base_z_mm',0.)])),
        quat=f'{math.cos(yaw)} 0 0 {math.sin(yaw)}')
    root.append(arm)
    ET.SubElement(root,'site',name='arm3_preview_origin',type='sphere',size='.008',rgba='.95 .55 .12 1')
    from .configuration import model_tcp,tcp_matrix
    tcp=tcp_matrix(placement.get('other_arm_tcp') or placement.get('tcp') or model_tcp())
    quat=Rotation.from_matrix(tcp[:3,:3]).as_quat()[[3,0,1,2]]
    gripper=arm.find(".//body[@name='arm3_preview_gripper_frame_link']")
    ET.SubElement(gripper,'site',name='arm3_preview_tcp',type='sphere',size='.004',
        pos=' '.join(str(x/1000) for x in tcp[:3,3]),quat=' '.join(map(str,quat)),rgba='.05 .62 .72 1')


def place_active_arm(scene,placement):
    if not placement:return
    active=placement.get('active_arm_id','arm2')
    if active=='arm2' and not placement.get('arm2_base_z_mm',0.):return
    import xml.etree.ElementTree as ET
    world=scene.find('worldbody');arm=world.find("body[@name='base_link']")
    world.remove(arm);yaw=math.radians(placement['base_yaw_deg'] if active=='arm3' else 0.)/2
    position=placement['base_xyz_mm'] if active=='arm3' else [0,0,placement.get('arm2_base_z_mm',0.)]
    wrapper=ET.SubElement(world,'body',name='active_arm_frame',pos=' '.join(str(v/1000) for v in position),quat=f'{math.cos(yaw)} 0 0 {math.sin(yaw)}')
    wrapper.append(arm)


def jig_pose_in_world(placement,xyz,yaw):
    if not placement:return list(xyz),yaw
    if placement.get('active_arm_id')!='arm3':return [*xyz[:2],xyz[2]+placement.get('arm2_base_z_mm',0.)],yaw
    import numpy as np
    matrix=base_transform(placement)
    return (matrix[:3,:3]@np.array(xyz)+matrix[:3,3]).tolist(),yaw+placement['base_yaw_deg']
