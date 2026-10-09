"""Keep teaching headings independent of a mesh's local +X convention."""

def mesh_yaw_offset(value):
    offset=value.get('mesh_yaw_offset_deg',0.)
    if type(offset) not in (int,float) or offset not in (0.,180.):
        raise ValueError('지그 모델 방향 보정은 0° 또는 180°여야 합니다.')
    return float(offset)

def pose_in_reference_basis(reference,current):
    pose=list(current['pose'])
    pose[2]+=mesh_yaw_offset(current)-mesh_yaw_offset(reference)
    return pose
