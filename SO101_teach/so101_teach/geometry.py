"""One coordinate definition for visualisation and mathematical validation.

No calibration guessing, mesh fitting or conversion through UI percentages.
"""
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from .domain import ROOT,JOINTS

class Kinematics:
    def __init__(self,reference,urdf=None,tcp=None):
        self.reference=reference
        from .configuration import tcp_matrix
        self.tool=tcp_matrix(tcp) if tcp is not None else np.eye(4)
        root=ET.parse(urdf or ROOT/'assets/so101/so101_modified.urdf').getroot()
        by_child={j.find('child').get('link'):j for j in root.findall('joint')}
        self.chain=[];link='gripper_frame_link'
        while link in by_child:
            j=by_child[link];o=j.find('origin');t=np.eye(4)
            if o is not None:
                t[:3,3]=np.fromstring(o.get('xyz','0 0 0'),sep=' ')*1000
                t[:3,:3]=Rotation.from_euler('xyz',np.fromstring(o.get('rpy','0 0 0'),sep=' ')).as_matrix()
            axis=j.find('axis');a=np.fromstring(axis.get('xyz'),sep=' ') if axis is not None else np.array([0.,0.,1.])
            self.chain.append((j.get('name'),j.get('type'),t,a));link=j.find('parent').get('link')
        self.chain.reverse()
    def fk(self,ticks):
        return self.fk_angles(self.reference.angles(ticks))
    def fk_angles(self,angles):
        q=dict(zip(JOINTS,angles));t=np.eye(4)
        for name,kind,origin,axis in self.chain:
            t=t@origin
            if kind!='fixed':
                turn=np.eye(4);turn[:3,:3]=Rotation.from_rotvec(axis*q[name]).as_matrix();t=t@turn
        return t@self.tool

def transform_jig_pose(tcp,old_jig,new_jig):
    """SE(2) change: translate/rotate XY and orientation, retain taught Z."""
    old=np.asarray(old_jig,dtype=float);new=np.asarray(new_jig,dtype=float)
    if old.shape!=(3,) or new.shape!=(3,) or not np.isfinite([*old,*new]).all():raise ValueError('지그 X·Y·방향 3개 값이 필요합니다.')
    a=new[2]-old[2];r=Rotation.from_euler('z',a,degrees=True).as_matrix()
    result=np.array(tcp,dtype=float,copy=True)
    if result.shape!=(4,4) or not np.isfinite(result).all():raise ValueError('TCP 변환 행렬 오류')
    result[:2,3]=new[:2]+r[:2,:2]@(result[:2,3]-old[:2]);result[:3,:3]=r@result[:3,:3]
    return result

def jig_delta(reference,current,symmetry=90):
    """Use the nearest equivalent direction for symmetric docking fixtures."""
    old=np.asarray(reference,float);new=np.array(current,float,copy=True)
    if old.shape!=(3,) or new.shape!=(3,) or not np.isfinite([*old,*new]).all() or symmetry not in (90,180,360):
        raise ValueError('지그 기준 X·Y·각도 형식 오류')
    new[2]=old[2]+(new[2]-old[2]+symmetry/2)%symmetry-symmetry/2
    return new


def _initial_corrected_step(kin,step,current_reference,mesh_sha):
    """Resolve one taught pose; J6 is invariant. No motors are accessed."""
    from scipy.optimize import least_squares
    cal=kin.reference.calibration;ticks=cal.ticks(step['ticks'])
    if not step.get('jig_id'):return {'ticks':ticks,'corrected':False}
    reference=step.get('jig_reference')
    if not reference or reference.get('stl_sha256')!=mesh_sha:raise ValueError('스텝에 저장된 지그 STL과 현재 지그가 다릅니다.')
    if not current_reference:raise ValueError('카메라에서 지그를 먼저 감지하세요.')
    if current_reference.get('stl_sha256')!=mesh_sha:raise ValueError('현재 측정값의 지그 STL이 등록 모델과 다릅니다.')
    if current_reference.get('symmetry_deg')!=reference['symmetry_deg']:raise ValueError('지그 방향 기준이 다릅니다.')
    from .jig_heading import pose_in_reference_basis
    actual=jig_delta(reference['pose'],pose_in_reference_basis(reference,current_reference),reference['symmetry_deg'])
    taught=kin.fk(ticks);target=transform_jig_pose(taught,reference['pose'],actual)
    if np.allclose(taught,target,rtol=0,atol=1e-9):return {'ticks':ticks,'corrected':True,'position_error_mm':0.,'orientation_error_deg':0.}
    q=np.array(kin.reference.angles(ticks));lower=[];upper=[]
    for n in JOINTS[:5]:
        low,high=kin.reference.angle_limits(n);lower.append(low);upper.append(high)
    def forward(x):return kin.fk_angles(np.r_[x,q[5]])
    def residual(x):
        pose=forward(x)
        position=pose[:3,3]-target[:3,3]
        attitude=Rotation.from_matrix(target[:3,:3].T@pose[:3,:3]).as_rotvec()
        return np.r_[position*[1,1,2],attitude*10,(x-q[:5])*.05]
    def solve(lo,hi,seed):
        result=least_squares(residual,np.clip(seed,lo,hi),bounds=(lo,hi),max_nfev=180,ftol=1e-9,xtol=1e-9,gtol=1e-9)
        values=kin.reference.ticks_from_angles(np.r_[result.x,q[5]]);values['gripper']=ticks['gripper']
        return values
    lower=np.array(lower);upper=np.array(upper)
    def evaluate(values):
        pose=kin.fk(values);angles=np.array(kin.reference.angles(values))[:5]
        margins=np.degrees(np.minimum(angles-lower,upper-angles))
        error=float(np.linalg.norm(pose[:3,3]-target[:3,3]))
        angle=float(np.degrees(Rotation.from_matrix(target[:3,:3].T@pose[:3,:3]).magnitude()))
        return margins,error,angle
    solved=solve(lower,upper,q[:5]);base_margins,base_error,base_angle=evaluate(solved)
    reserve=np.deg2rad(3.)
    if min(base_margins)<2.9:
        # All J1..J5 are re-solved together, never independently clamped.
        # A narrow calibrated range can only supply half of its span.
        maximum_reserve=np.minimum(reserve,(upper-lower)*.49)
        trials=[maximum_reserve]
        # If a joint is intrinsically constrained, still seek clearance for
        # the others. Also try smaller shared reserves before giving up.
        near=np.flatnonzero(base_margins<2.9)
        for i in near:
            partial=maximum_reserve.copy();partial[i]=0.;trials.append(partial)
        trials.extend(np.minimum(maximum_reserve,np.deg2rad(d)) for d in (1.,.5))
        if len(near)>2:
            for i in near:
                partial=maximum_reserve.copy();partial[near]=0.;partial[i]=maximum_reserve[i];trials.append(partial)
        def rank(values,assessment):
            margins,error,angle=assessment
            return (int(np.count_nonzero(margins<.15)),float(np.maximum(0.,3.-margins).sum()),
                    angle,error,float(np.linalg.norm(np.array(kin.reference.angles(values))[:5]-q[:5])))
        best_rank=rank(solved,(base_margins,base_error,base_angle))
        seed=np.array(kin.reference.angles(solved))[:5]
        for clearance in trials:
            candidate=solve(lower+clearance,upper-clearance,seed)
            assessment=evaluate(candidate);margins,error,angle=assessment
            # Clearance must not hide an inaccurate pose. Allow at most one
            # extra degree versus the original solution, including rounding.
            if error>1. or angle>min(45.,base_angle+1.):continue
            candidate_rank=rank(candidate,assessment)
            if candidate_rank<best_rank:solved=candidate;best_rank=candidate_rank
            if min(margins)>=2.9:break
    pose=kin.fk(solved);error=float(np.linalg.norm(pose[:3,3]-target[:3,3]))
    angle=float(np.degrees(Rotation.from_matrix(target[:3,:3].T@pose[:3,:3]).magnitude()))
    tilt=float(np.degrees(np.arccos(np.clip(target[:3,2]@pose[:3,2],-1,1))))
    limits=[n for n in JOINTS[:5] if min(solved[n]-cal.motors[n].low,cal.motors[n].high-solved[n])<=2]
    margins,_,_=evaluate(solved)
    joint_margins={n:float(v) for n,v in zip(JOINTS[:5],margins)}
    margin=joint_margins['wrist_flex']
    # Do not silently run an unreachable target or flip the fixed contact jaw.
    if error>3 or angle>45:raise ValueError(f"{step['name']}: 지그 보정 도달 불가 (위치 오차 {error:.1f}mm, 자세 차이 {angle:.1f}°)")
    return {'ticks':solved,'corrected':True,'position_error_mm':error,'orientation_error_deg':angle,
            'tilt_error_deg':tilt,'limiting_joints':limits,
            'wrist_margin_deg':margin,'wrist_margin_target_deg':3.,'wrist_margin_satisfied':margin>=2.9,
            'joint_margins_deg':joint_margins,'joint_margin_target_deg':3.,
            'joint_margin_satisfied':bool(min(margins)>=2.9),
            'low_margin_joints':[n for n,v in joint_margins.items() if v<2.9],
            'target_xyz_mm':target[:3,3].tolist(),'actual_xyz_mm':pose[:3,3].tolist()}


# Soft pose preferences in servo-angle space. Position/rotation acceptance and
# measured mechanical bounds remain independent of these weights.
POSTURE_JOINT_WEIGHTS=np.array([1.,2.,2.,1.,.5])
POSTURE_REFERENCE_WEIGHT=.002
POSTURE_CONTINUITY_WEIGHT=.001


def _posture_metrics(kin,ticks,target,taught,previous=None):
    pose=kin.fk(ticks);q=np.array(kin.reference.angles(ticks))[:5]
    delta=pose[:3,:3]@target[:3,:3].T
    rotation=Rotation.from_matrix(delta).as_rotvec()
    plane=float(np.degrees(np.arccos(np.clip(delta[2,2],-1.,1.))))
    return {'position':float(np.linalg.norm(pose[:3,3]-target[:3,3])),
            'rotation':float(np.degrees(np.linalg.norm(rotation))), 'plane':plane,
            'angle_vector':rotation,'angles':q}


def corrected_step(kin,step,current_reference,mesh_sha,*,previous_ticks=None,previous_taught_ticks=None):
    """Preserve taught grasp tilt; optimise only an actual SE(2) jig change.

    No absolute elbow-height preference, no forced tool verticalisation, and no
    object-centre/TCP substitution. A taught horizontal part plane is affected
    only by the orientation residual relative to the transformed taught pose.
    """
    from scipy.optimize import minimize
    from .jig_compatibility import execution_reference
    original=step.get('jig_reference');resolved=execution_reference(original,current_reference,mesh_sha) if step.get('jig_id') else original
    compatible=resolved is not original
    if compatible:step={**step,'jig_reference':resolved}
    base=_initial_corrected_step(kin,step,current_reference,mesh_sha)
    if compatible:base['jig_reference_compatibility']='legacy_carrier_to_assembly' if original.get('symmetry_deg')==180 else 'carrier_b_top_update'
    if not step.get('jig_id'):return base
    taught_ticks=step['ticks'];taught=kin.fk(taught_ticks);r=step['jig_reference']
    from .jig_heading import pose_in_reference_basis
    target=transform_jig_pose(taught,r['pose'],jig_delta(r['pose'],pose_in_reference_basis(r,current_reference),r['symmetry_deg']))
    if np.allclose(taught,target,rtol=0,atol=1e-9):return base
    q=np.array(kin.reference.angles(taught_ticks));bounds=np.array([kin.reference.angle_limits(n) for n in JOINTS[:5]])
    lo,hi=bounds[:,0],bounds[:,1];base_metrics=_posture_metrics(kin,base['ticks'],target,taught)
    base_q=base_metrics['angles'];base_margins=np.minimum(base_q-lo,hi-base_q)
    # Preserve clearances already achieved, instead of trading them for attitude.
    clearance=np.minimum(np.deg2rad(3.),np.maximum(0.,base_margins-np.deg2rad(.1)))
    lower,upper=lo+clearance,hi-clearance
    expected=q[:5].copy();previous=None
    if previous_ticks is not None and previous_taught_ticks is not None:
        previous=np.array(kin.reference.angles(previous_ticks))[:5]
        prior_reference=np.array(kin.reference.angles(previous_taught_ticks))[:5]
        expected+=previous-prior_reference
    def pose_of(x):return kin.fk_angles(np.r_[x,q[5]])
    def objective(x):
        rotation=Rotation.from_matrix(pose_of(x)[:3,:3]@target[:3,:3].T).as_rotvec()
        return float(np.dot(rotation*np.array([2.,2.,1.]),rotation*np.array([2.,2.,1.]))
                     +POSTURE_REFERENCE_WEIGHT*np.sum(POSTURE_JOINT_WEIGHTS*(x-q[:5])**2)
                     +POSTURE_CONTINUITY_WEIGHT*np.sum((x-expected)**2))
    constraint={'type':'eq','fun':lambda x:(pose_of(x)[:3,3]-target[:3,3])/100.}
    seeds=[base_q]
    if previous is not None:seeds.append(np.clip(previous,lower,upper))
    seeds.append(np.clip(q[:5],lower,upper))
    best=base['ticks'];best_score=objective(base_q);best_metrics=base_metrics;used=0
    position_limit=min(3.,max(.5,base_metrics['position']+.05))
    for seed in seeds:
        if used and np.linalg.norm(seed-seeds[0])<1e-7:continue
        used+=1
        result=minimize(objective,np.clip(seed,lower,upper),method='SLSQP',bounds=list(zip(lower,upper)),
                        constraints=[constraint],options={'maxiter':90,'ftol':1e-10})
        if not result.success or not np.isfinite(result.x).all():continue
        try:
            ticks=kin.reference.ticks_from_angles(np.r_[result.x,q[5]]);ticks['gripper']=taught_ticks['gripper']
        except ValueError:continue
        m=_posture_metrics(kin,ticks,target,taught)
        if m['position']>position_limit or m['rotation']>min(45.,base_metrics['rotation']+1.) or m['plane']>base_metrics['plane']+.1:continue
        margins=np.minimum(m['angles']-lo,hi-m['angles'])
        if np.any(margins<clearance-np.deg2rad(.1)):continue
        score=objective(m['angles'])
        if score<best_score-1e-10:best=ticks;best_score=score;best_metrics=m
    pose=kin.fk(best);margins=np.degrees(np.minimum(best_metrics['angles']-lo,hi-best_metrics['angles']))
    out=dict(base,ticks=best,position_error_mm=best_metrics['position'],orientation_error_deg=best_metrics['rotation'],
             tilt_error_deg=float(np.degrees(np.arccos(np.clip(target[:3,2]@pose[:3,2],-1.,1.)))),
             target_xyz_mm=target[:3,3].tolist(),actual_xyz_mm=pose[:3,3].tolist(),
             taught_plane_tilt_deg=best_metrics['plane'],posture_reference='transformed_taught_grasp',
             posture_optimized=best!=base['ticks'],posture_seed_count=used,
             joint_change_from_teaching_deg={n:float(v) for n,v in zip(JOINTS[:5],np.degrees(best_metrics['angles']-q[:5]))},
             joint_change_from_previous_deg=None if previous is None else {n:float(v) for n,v in zip(JOINTS[:5],np.degrees(best_metrics['angles']-previous))},
             posture_note='티칭 기울기 유지' if best_metrics['plane']<=1. else '현재 계산 해에서는 티칭 기울기 차이가 남습니다(위치·관절 범위 우선).')
    out.update(joint_margins_deg={n:float(v) for n,v in zip(JOINTS[:5],margins)},
               limiting_joints=[n for n in JOINTS[:5] if min(best[n]-kin.reference.calibration.motors[n].low,kin.reference.calibration.motors[n].high-best[n])<=2],
               joint_margin_satisfied=bool(min(margins)>=2.9),low_margin_joints=[n for n,v in zip(JOINTS[:5],margins) if v<2.9],
               wrist_margin_deg=float(margins[3]),wrist_margin_satisfied=bool(margins[3]>=2.9))
    return out


def corrected_plan(kin,steps,current,mesh_sha_for):
    """One solver for PC preview and Pi execution, seeded from the prior result."""
    plan=[];previous=None;previous_taught=None
    for step in steps:
        key=step.get('jig_id')
        reference=current if current and 'pose' in current else (current or {}).get(key)
        result=corrected_step(kin,step,reference,mesh_sha_for(key) if key else '',
                              previous_ticks=previous,previous_taught_ticks=previous_taught)
        plan.append(result);previous=result['ticks'];previous_taught=step['ticks']
    return plan
