"""Selected-arm identity and display transforms; no hardware access."""
from copy import deepcopy
from pathlib import Path
import numpy as np

ARM_NAMES={'arm2':'로봇팔2','arm3':'로봇팔3'}


def arm_id(profile):
    value=profile.get('robot_id','arm2')
    if value not in ARM_NAMES:raise ValueError('지원하지 않는 로봇팔 구분입니다.')
    return value


def calibration_pending(profile):return profile.get('calibration_status')=='pending'


def require_calibrated(profile):
    if calibration_pending(profile):raise ValueError(ARM_NAMES[arm_id(profile)]+'의 새 3점 보정을 먼저 완료하세요.')


def progress_path(data_dir,role,profile):
    # Existing arm2 and shared PC leader progress retain their original paths.
    suffix='-arm3' if role=='follower' and arm_id(profile)=='arm3' else ''
    return Path(data_dir)/'calibration_progress'/(role+suffix+'.json')


def world_from_base(profile):
    matrix=np.array(profile.get('world_from_base',np.eye(4)),dtype=float)
    if matrix.shape!=(4,4) or not np.isfinite(matrix).all():raise ValueError('로봇팔 배치 행렬 오류')
    if not np.allclose(matrix[3],[0,0,0,1]) or not np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3),atol=1e-6) or np.linalg.det(matrix[:3,:3])<.999:
        raise ValueError('로봇팔 배치 회전 오류')
    return matrix


def preview_profile(profile):
    """Camera view uses common workcell coordinates; planning retains arm-local data."""
    value=deepcopy(profile);transform=world_from_base(profile)
    if profile.get('extrinsics',{}).get('base_from_camera') is not None:
        value['extrinsics']['base_from_camera']=(transform@np.array(profile['extrinsics']['base_from_camera'])).tolist()
    value['table_z_mm']=float(profile.get('table_z_mm',-7.4))+float(transform[2,3])
    value.pop('world_from_base',None)
    return value


def camera_reference_for_profile(profile,reference):
    """Stored CAD camera reference is in the shared workcell (arm2) frame."""
    value=deepcopy(reference)
    value['base_from_camera']=(np.linalg.inv(world_from_base(profile))@np.asarray(reference['base_from_camera'],float)).tolist()
    return value


def scene_placement(placement,profile,other_angles=None):
    if placement is None:return None
    value=deepcopy(placement);value['active_arm_id']=arm_id(profile)
    z=float(world_from_base(profile)[2,3])
    if arm_id(profile)=='arm3':value['base_xyz_mm'][2]=z
    else:value['arm2_base_z_mm']=z
    if other_angles is not None:value['other_arm_joint_angles_rad']=list(other_angles)
    return value


def tcp_positions(active_fk,profile,placement):
    from .geometry import Kinematics
    from .workcell_preview import base_transform
    active=arm_id(profile);other='arm2' if active=='arm3' else 'arm3'
    result={active:(world_from_base(profile)@active_fk)[:3,3]}
    if placement:
        matrix=np.eye(4) if other=='arm2' else base_transform(placement)
        if other=='arm2':matrix[2,3]=placement.get('arm2_base_z_mm',0.)
        kin=Kinematics(None,tcp=placement.get('other_arm_tcp') or placement.get('tcp'))
        angles=placement.get('other_arm_joint_angles_rad',placement['joint_angles_rad'])
        result[other]=(matrix@kin.fk_angles(angles))[:3,3]
    return result
