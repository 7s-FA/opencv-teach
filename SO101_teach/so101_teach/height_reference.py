"""Public heights are above the floor; kinematics stay in the robot base frame."""
from copy import deepcopy
import math

DEFAULT_FLOOR_Z_MM=-7.4  # Legacy absolute-plane fallback includes the 5 mm mount.
DIRECT_BASE_FLOOR_Z_MM=-2.4  # Bare base underside, before the mounting plate.

def floor_z(profile):
    z=float(profile.get('table_z_mm',DEFAULT_FLOOR_Z_MM))
    if not math.isfinite(z):raise ValueError('바닥 기준면 보정을 확인하세요.')
    return z

def floor_adjustment(profile):
    """Positive installation height raises the arm origin above the work surface."""
    return DIRECT_BASE_FLOOR_Z_MM-floor_z(profile)

def floor_from_adjustment(height):
    z=DIRECT_BASE_FLOOR_Z_MM-float(height)
    if not math.isfinite(z) or not -100<=z<=100:raise ValueError('로봇팔 높이 보정값이 유효 범위를 벗어났습니다.')
    return z

def profile_with_arm_height(profile,height):
    """Move the selected origin, preserving the shared work surface and camera."""
    from .arm_workspace import world_from_base
    value=deepcopy(profile);z=floor_from_adjustment(height);delta=floor_z(profile)-z
    transform=world_from_base(profile).copy();transform[2,3]+=delta
    value['world_from_base']=transform.tolist();value['table_z_mm']=z
    camera=value.get('extrinsics',{}).get('base_from_camera')
    if camera is not None:camera[2][3]-=delta
    return value

def restore_model_floor(profile,saved):
    z=float(saved['table_z_mm'])
    if not math.isfinite(z) or not -100<=z<=100:raise ValueError('저장된 바닥 기준면 보정이 올바르지 않습니다.')
    if saved.get('height_reference')=='arm_origin_up':
        return profile_with_arm_height(profile,DIRECT_BASE_FLOOR_Z_MM-z)
    value=deepcopy(profile);value['table_z_mm']=z
    return value

def base_z_from_height(height,profile):return floor_z(profile)+float(height)

def height_from_base_z(z,profile):return float(z)-floor_z(profile)

def support_height(config,profile):
    if 'support_height_mm' in config:
        value=config['support_height_mm'];value=0. if value is None or value=='' else float(value)
    else:
        legacy=config.get('support_z_mm')
        value=0. if legacy is None or legacy=='' else height_from_base_z(legacy,profile)
    if not math.isfinite(value) or not -500<=value<=1500:raise ValueError('받침 높이는 바닥 기준 -500~1500mm로 입력하세요.')
    return value

def normalize_jig_height(config,profile):
    item=deepcopy(config);item['support_height_mm']=support_height(config,profile);item.pop('support_z_mm',None)
    return item

def support_bottom_z(config,profile):return base_z_from_height(support_height(config,profile),profile)

def support_plane_profile(profile,config):return {**profile,'table_z_mm':support_bottom_z(config,profile)}

def rim_height(mesh):return float(mesh['rim_z_mm'])-float(mesh.get('low_mm',[0.,0.,0.])[2])

def detection_plane_z(plane_profile,mesh):
    # plane_profile.table_z_mm already denotes this jig's supporting plane.
    return floor_z(plane_profile)+rim_height(mesh)
