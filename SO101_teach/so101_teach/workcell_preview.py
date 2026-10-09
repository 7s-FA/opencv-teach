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


def load_placement(data_dir):
    path=Path(data_dir)/'workcell-preview.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    if value.get('schema')!=1 or value.get('display_only') is not True:
        raise ValueError('작업대 배치는 표시 전용 설정이어야 합니다.')
    if value.get('robot_id')!='arm3' or value.get('source')!='camera_visual_estimate':
        raise ValueError('지원하지 않는 작업대 배치 설정입니다.')
    for key,count in (('base_xyz_mm',3),('joint_angles_rad',6)):
        row=value.get(key)
        if not isinstance(row,list) or len(row)!=count or any(type(v) not in (int,float) or not math.isfinite(v) for v in row):
            raise ValueError('작업대 배치 수치 오류: '+key)
    if type(value.get('base_yaw_deg')) not in (int,float) or not math.isfinite(value['base_yaw_deg']):
        raise ValueError('작업대 방향 수치 오류')
    from .configuration import model_tcp,tcp_matrix
    value.setdefault('tcp',model_tcp())
    tcp_matrix(value['tcp'])
    board=value.get('board')
    if board:
        for key,count in (('size_mm',2),('center_xy_mm',2)):
            row=board.get(key)
            if not isinstance(row,list) or len(row)!=count or not all(type(x) in (int,float) and math.isfinite(x) for x in row):raise ValueError('흰판 배치 수치 오류')
        if min(board['size_mm'])<=0:raise ValueError('흰판 크기는 양수여야 합니다.')
        for key in ('yaw_deg','top_z_mm','visual_thickness_mm'):
            if type(board.get(key)) not in (int,float) or not math.isfinite(board[key]):raise ValueError('흰판 높이·방향 수치 오류')
        if board['visual_thickness_mm']<=0:raise ValueError('흰판 표시 두께는 양수여야 합니다.')
        legs=board.get('legs')
        if legs:
            centers=legs.get('centers_xy_mm')
            if not isinstance(centers,list) or len(centers)!=4 or any(not isinstance(xy,list) or len(xy)!=2 or any(type(x) not in (int,float) or not math.isfinite(x) for x in xy) for xy in centers):raise ValueError('작업대 다리 4개의 중심 좌표를 확인하세요.')
            if legs.get('height_mm')!=185:raise ValueError('현재 다리 조립 모델의 높이는 185mm입니다.')
            enclosure=board.get('enclosure')
            if enclosure:
                thickness=enclosure.get('panel_thickness_mm')
                if type(thickness) not in (int,float) or not math.isfinite(thickness) or not 0<thickness<min(board['size_mm'])/2:raise ValueError('작업대 옆판 두께를 확인하세요.')
            backdrop=board.get('backdrop')
            if backdrop:
                for field in ('height_mm','width_mm','thickness_mm','gap_mm'):
                    if type(backdrop.get(field)) not in (int,float) or not math.isfinite(backdrop[field]) or backdrop[field]<0:raise ValueError('뒤쪽 흰판의 치수를 확인하세요.')
                if min(backdrop['height_mm'],backdrop['width_mm'],backdrop['thickness_mm'])<=0:raise ValueError('뒤쪽 흰판 크기는 양수여야 합니다.')
    linear=value.get('linear_stage')
    if linear:
        row=linear.get('position_mm')
        if not isinstance(row,list) or len(row)!=3 or not all(type(x) in (int,float) and math.isfinite(x) for x in row):raise ValueError('리니어 위치 수치 오류')
        for key in ('yaw_deg','stroke_mm'):
            if type(linear.get(key)) not in (int,float) or not math.isfinite(linear[key]):raise ValueError('리니어 방향·변위 수치 오류')
        if not 0<=linear['stroke_mm']<=100:raise ValueError('L12 표시 변위는 0~100mm여야 합니다.')
        for key in ('carriage_home_x_mm','front_pin_x_mm','rear_eye_x_mm','actuator_axis_z_mm','actuator_body_roll_deg','actuator_rear_axis_z_mm'):
            if key in linear and (type(linear[key]) not in (int,float) or not math.isfinite(linear[key])):raise ValueError('리니어 설치 수치 오류: '+key)
        if linear.get('front_connector_model') not in (None,'photo_observed','latest_top_mount'):raise ValueError('지원하지 않는 리니어 고정부 모델입니다.')
        if 'end_stop_position_mm' in linear:
            row=linear['end_stop_position_mm']
            if not isinstance(row,list) or len(row)!=3 or not all(type(x) in (int,float) and math.isfinite(x) for x in row):raise ValueError('리니어 끝막이 위치 오류')
        endpoints=linear.get('endpoint_reference',{})
        forward=endpoints.get('forward',{});retracted=endpoints.get('retracted',{})
        if 'carriage_x_mm' in forward or 'carriage_x_mm' in retracted:
            for row in (forward,retracted):
                if any(type(row.get(k)) not in (int,float) or not math.isfinite(row[k]) for k in ('commanded_mm','carriage_x_mm')):raise ValueError('리니어 전후진 기준 수치 오류')
                if not 0<=row['commanded_mm']<=100:raise ValueError('리니어 전후진 명령 범위 오류')
            if forward['commanded_mm']<=retracted['commanded_mm'] or forward['carriage_x_mm']<=retracted['carriage_x_mm']:raise ValueError('리니어 전후진 기준 순서 오류')
    return value


def base_transform(placement):
    """Arm2/world from arm3 base, in mm. TCP offsets stay in the gripper frame."""
    import numpy as np
    from scipy.spatial.transform import Rotation
    matrix=np.eye(4);matrix[:3,:3]=Rotation.from_euler('z',placement['base_yaw_deg'],degrees=True).as_matrix()
    matrix[:3,3]=placement['base_xyz_mm']
    return matrix


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
