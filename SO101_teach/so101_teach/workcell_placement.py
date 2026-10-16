"""Validated workcell coordinates shared by inspection and desktop rendering."""
import json
import math
from pathlib import Path


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
