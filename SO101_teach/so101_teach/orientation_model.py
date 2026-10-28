"""Disambiguate carrier half-turns using its circular opening, then its fixed central junction."""
import cv2
import numpy as np
from .height_reference import detection_plane_z


def orient_carrier(frame,mesh,allowed,profile,result,*,evidence=None,junction_source=None,recover_carrier=False):
    from .vision import project_plane,world_xy,projected_jig_axes
    feature=mesh.get('assembly',{}).get('orientation_hole');candidate=result.get('selected')
    if not feature or not candidate or not candidate.get('metric') or not profile:return result
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    gray=evidence.source.blurred()
    distance=evidence.distance(15,45)
    angle=np.linspace(0,2*np.pi,64,endpoint=False);circle=np.c_[np.cos(angle),np.sin(angle)]
    shift=np.stack(np.meshgrid(np.arange(-3,3.1),np.arange(-3,3.1)),axis=-1).reshape(-1,2)
    center=np.asarray(candidate['metric']['center_xy_mm']);scale=float(candidate.get('boundary_scale',1.))
    local=np.asarray(feature['center_mm'])-np.asarray(mesh['low_mm'][:2])-np.asarray(mesh['size_mm'][:2])/2
    z=detection_plane_z(profile,mesh)+feature['z_mm']-mesh['rim_z_mm'];hypotheses=[]
    for turn in (0,180):
        yaw=(candidate['metric']['yaw_deg']+turn)%360;a=np.deg2rad(yaw);rotation=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
        xy=local@rotation.T*scale+center
        uv=project_plane(np.concatenate([xy+circle*feature[k]*scale for k in ('inner_radius_mm','outer_radius_mm')]),profile,z).reshape(2,64,2)
        pixel=np.rint(uv[None,:,:,:]+shift[:,None,None,:]).astype(int);h,w=allowed.shape
        inside=(pixel[:,:,:,0]>=0)&(pixel[:,:,:,0]<w)&(pixel[:,:,:,1]>=0)&(pixel[:,:,:,1]<h)
        px=np.clip(pixel[:,:,:,0],0,w-1);py=np.clip(pixel[:,:,:,1],0,h-1);inside &=allowed[py,px]>0
        d=distance[py,px];cost=np.minimum(d,4).mean((1,2))/4;cost[~inside.all((1,2))]=np.inf
        # Optimize within the alignment tolerance, rather than rejecting the
        # whole direction because a slightly cheaper out-of-bounds shift won.
        observed_centers=project_plane([xy],profile,z)[0]+shift
        errors=np.linalg.norm(world_xy(observed_centers,profile,z)-xy,axis=1)
        cost[errors>4]=np.inf
        # A cheaper shift can align a short arc while missing sectors that a
        # neighbouring shift observes. Minimize over shifts that satisfy the
        # existing coverage checks; do not discard that evidence afterwards.
        all_support=(d<1.8).reshape(-1,2,8,8).mean(3)
        sectors=np.count_nonzero(all_support.min(1)>=.5,axis=1)
        eligible=(sectors>=6)&(all_support.mean((1,2))>=.65)&np.isfinite(cost)
        raw_cost=float(cost.min())
        best=int(np.argmin(np.where(eligible,cost,np.inf))) if np.any(eligible) else int(np.argmin(cost))
        support=all_support[best]
        observed=project_plane([xy],profile,z)[0]+shift[best]
        error=float(np.linalg.norm(world_xy([observed],profile,z)[0]-xy))
        hypotheses.append({'yaw':float(yaw),'cost':float(cost[best]),'raw_cost':raw_cost,'support':support.tolist(),'sectors':int(sectors[best]),'center_px':observed,'outline_px':uv[1]+shift[best],'error_mm':error})
    hypotheses.sort(key=lambda v:v['cost']);best,other=hypotheses
    # Keep the opposite heading's cheapest evidence as the ambiguity bound,
    # even if its best shift has insufficient coverage. Filtering must not
    # artificially increase the separation between two possible headings.
    candidate['orientation_scores']=[{'yaw_deg':h['yaw'],'cost':cost if np.isfinite(cost) else None,'supported_sectors':h['sectors']}
                                     for h,cost in ((best,best['cost']),(other,other['raw_cost']))]
    verified=best['cost']<=.30 and best['sectors']>=6 and np.mean(best['support'])>=.65 and best['error_mm']<=4 and other['raw_cost']-best['cost']>=.12
    junction=None
    if not verified:
        from .junction_orientation import evaluate
        try:
            if junction_source is None:junction=evaluate(frame,mesh,allowed,profile,candidate)
            else:
                from .roi_geometry import roi_mask
                raw_frame,raw_roi,raw_profile=junction_source
                raw_allowed=roi_mask(raw_roi,raw_frame.shape[1],raw_frame.shape[0])
                junction=evaluate(raw_frame,mesh,raw_allowed,raw_profile,candidate,outline_profile=profile)
            if recover_carrier:
                from .carrier_cad import resolve
                if junction_source is None:junction=resolve(frame,mesh,allowed,profile,candidate,junction)
                else:junction=resolve(raw_frame,mesh,raw_allowed,raw_profile,candidate,junction)
        except (OSError,ValueError,KeyError,cv2.error) as exc:
            junction={'verified':False,'scores':[],'reason':str(exc)}
        candidate['orientation_junction_scores']=junction['scores']
        for key in ('cad_direction_verification','orientation_corroborated_by_cad','cad_direction_scores'):
            if key in junction:candidate[key]=junction[key]
        if not junction['verified']:
            candidate['orientation_verified']=False
            return {**result,'selected':None,'status':'orientation_unconfirmed','error':'원형 기준과 접합부로 앞뒤 방향을 확인하지 못했습니다. '+junction['reason']}
        best={'yaw':junction['yaw_deg']}
        candidate.update(orientation_method='central_junction',orientation_junction_outline_px=junction['outline_px'],
                         orientation_junction_score=junction['score'],orientation_junction_margin=junction['margin'])
        if junction.get('method')=='cad_fixed_features':
            candidate['orientation_method']='cad_fixed_features'
            for key in ('orientation_junction_scores','orientation_junction_outline_px','orientation_junction_score','orientation_junction_margin'):candidate.pop(key,None)
    else:
        candidate.update(orientation_method='circular_opening',orientation_hole_px=best['center_px'].tolist(),
                         orientation_hole_outline_px=best['outline_px'].tolist(),orientation_hole_alignment_error_mm=best['error_mm'])
    # The original carrier teaching +X points toward the A fixture. Its new
    # assembly STL uses the opposite +X. Preserve that teaching zero while
    # retaining the full-turn cue for a real physical half-turn.
    heading=(best['yaw']-180.)%360
    candidate['metric'].update(yaw_deg=heading,symmetry_deg=360,mesh_yaw_offset_deg=180.)
    candidate.update(symmetry_deg=360,orientation_verified=True)
    # Preserve the same outer boundary while orienting its first edge to the
    # model's +X axis. Raw-pixel restoration must not fold the half-turn away.
    quad=np.asarray(candidate['quad']);xy=world_xy(quad[:2],profile,detection_plane_z(profile,mesh));edge=xy[1]-xy[0]
    direction=np.degrees(np.arctan2(edge[1],edge[0]))%360
    if abs((direction-heading+180)%360-180)>90:quad=np.roll(quad,2,axis=0)
    candidate['quad']=quad.tolist();edge=quad[1]-quad[0];candidate['image_angle_deg']=float(np.degrees(np.arctan2(edge[1],edge[0]))%360)
    candidate['axes_px']=projected_jig_axes(candidate,profile,detection_plane_z(profile,mesh))
    result['orientation_period_deg']=360
    return result
