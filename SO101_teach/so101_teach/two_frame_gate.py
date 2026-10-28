"""Carrier observation checks and the archived two-frame replay policy.

This only accelerates acquisition; it never verifies a pose for robot motion.
Unsupported models, assets and incomplete evidence retain the ordinary window.
"""
import math
import numpy as np

MODEL_IDENTITY='134067656737d9882cec3d99a279834d8bb763902b72b034fd6f55d99836d272'
CARRIER_SHA256='23f8ddb6d925c8c15f5ec543804dc42c2db10bc356cfcdaa45742b1c0e04f4d9'

def pose_delta(a,b):
    x,y=a['metric'],b['metric'];period=a.get('symmetry_deg')
    if period!=360 or period!=b.get('symmetry_deg') or x.get('mesh_yaw_offset_deg',0)!=y.get('mesh_yaw_offset_deg',0):return math.inf,math.inf
    center=float(np.linalg.norm(np.asarray(x['center_xy_mm'])-y['center_xy_mm']))
    angle=abs((x['yaw_deg']-y['yaw_deg']+period/2)%period-period/2)
    return center,angle

def finite(value):
    if isinstance(value,dict):return all(finite(v) for v in value.values())
    if isinstance(value,(list,tuple)):return all(finite(v) for v in value)
    return not isinstance(value,(int,float,np.number)) or math.isfinite(value)

def quality(c):
    if not c or not finite(c) or not c.get('shape_match') or not c.get('outer_shape',{}).get('valid'):return 'shape'
    if c.get('partial_visible') or c.get('visible_fraction',0)<.99:return 'visibility'
    quad=np.asarray(c.get('quad'),float)
    if quad.shape!=(4,2) or np.any(quad<[2,2]) or np.any(quad>[1277,717]):return 'visibility'
    if not all(c.get(k) for k in ('observed_boundary_refined','grid_verified','orientation_verified')):return 'carrier_evidence'
    if c.get('symmetry_deg')!=360 or c.get('metric',{}).get('dimension_source')!='observed_boundary_fit':return 'carrier_heading_or_size'
    coverage=np.asarray(c.get('boundary_coverage'),float)
    if coverage.shape!=(4,) or coverage.min()<.5 or coverage.mean()<.8:return 'carrier_boundary'
    if c.get('boundary_corner_error_mm',math.inf)>1.5 or c.get('grid_alignment_error_mm',math.inf)>5 or c.get('orientation_hole_alignment_error_mm',math.inf)>4:return 'carrier_alignment'
    if not .98<=c.get('boundary_scale',0)<=1.04:return 'carrier_scale'
    scores=c.get('orientation_scores',[])
    if len(scores)!=2:return 'carrier_direction_margin'
    scores=sorted(scores,key=lambda x:x.get('cost',math.inf))
    if scores[0].get('cost',math.inf)>.25 or scores[0].get('supported_sectors',0)<6 or scores[1]['cost']-scores[0]['cost']<.15:return 'carrier_direction_margin'
    return None

def pair_reason(first,second,mesh,frame_shape):
    """Return None only when a same-frame independent CV check is warranted."""
    try:
        if tuple(frame_shape[:2])!=(720,1280) or mesh.get('shape')!='rectangle' or mesh.get('sha256')!=CARRIER_SHA256:return 'unsupported_context'
        if not first.get('mask_seed_refined') or any(r.get('detection_method')!='model_roi' for r in (first,second)):return 'not_ai_seeded'
        if any(r.get('region_model')!='n' or r.get('model_identity')!=MODEL_IDENTITY for r in (first,second)):return 'model'
        if first.get('tracking_state') not in ('acquired','reacquired') or second.get('tracking_state')!='tracking' or second.get('tracking_local_retry'):return 'tracking'
        for raw in (first,second):
            if raw.get('inspection_only') or raw.get('status')!='shape_match' or len(raw.get('candidates',[]))!=1:return 'ambiguous_or_unverified'
            reason=quality(raw.get('selected'))
            if reason:return reason
        a,b=first['selected'],second['selected'];center,angle=pose_delta(a,b)
        if not math.isfinite(center+angle) or center>.15 or angle>.1:return 'pair_motion'
        if min(a['area_px'],b['area_px'])<=0 or max(a['area_px'],b['area_px'])/min(a['area_px'],b['area_px'])>1.01:return 'area_change'
        return None
    except (KeyError,TypeError,ValueError,ZeroDivisionError):return 'incomplete_evidence'

def confirmation_reason(first,second,confirmation):
    """Compare full-ROI CV to both observations; this is not a third vote."""
    try:
        c=confirmation.get('selected')
        if not c or not finite(c) or confirmation.get('inspection_only') or confirmation.get('status')!='shape_match' or len(confirmation.get('candidates',[]))!=1 or not c.get('shape_match') or not c.get('outer_shape',{}).get('valid'):return 'independent_detection'
        if c.get('partial_visible') or c.get('visible_fraction',0)<.99:return 'independent_visibility'
        if not c.get('grid_verified') or not c.get('orientation_verified'):return 'independent_direction'
        for raw in (first,second):
            center,angle=pose_delta(raw['selected'],c)
            if not math.isfinite(center+angle) or center>.2 or angle>.2:return 'independent_disagreement'
        return None
    except (KeyError,TypeError,ValueError):return 'incomplete_confirmation'


def carrier_observation_reason(raw,mesh,frame_shape):
    """Require this image's validated OpenCV rim, grid and full-turn heading.

    The old two-frame policy below remains available to historical replay tools;
    live acquisition uses three distinct observations in StableCandidate.
    """
    try:
        if tuple(frame_shape[:2])!=(720,1280) or mesh.get('shape')!='rectangle' or mesh.get('sha256')!=CARRIER_SHA256:return 'unsupported_context'
        if raw.get('detection_method')!='model_roi' or raw.get('region_model')!='n' or raw.get('model_identity')!=MODEL_IDENTITY:return 'model'
        if raw.get('inspection_only') or raw.get('pose_held') or raw.get('status')!='shape_match' or len(raw.get('candidates',[]))!=1:return 'ambiguous_or_unverified'
        return quality(raw.get('selected'))
    except (KeyError,TypeError,ValueError):return 'incomplete_evidence'
