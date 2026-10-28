"""Bounded, subpixel rim refinement after independent model detection."""
from copy import deepcopy
import cv2
import numpy as np
from scipy.optimize import least_squares
from .height_reference import detection_plane_z


def sample(field,uv,outside=5.):
    """Continuous bilinear sampling (cv2.remap quantizes interpolation weights)."""
    h,w=field.shape;x,y=np.asarray(uv).T
    valid=(x>=0)&(x<w-1)&(y>=0)&(y<h-1)
    x=np.clip(x,0,w-2);y=np.clip(y,0,h-2)
    ix=x.astype(int);iy=y.astype(int);dx=x-ix;dy=y-iy
    value=(field[iy,ix]*(1-dx)+field[iy,ix+1]*dx)*(1-dy)+(field[iy+1,ix]*(1-dx)+field[iy+1,ix+1]*dx)*dy
    return np.where(valid,value,outside)


def refine_outer_boundary(frame,mesh,profile,allowed,visible,result,*,evidence=None,diagnostics=None,recover_carrier=False):
    options={'evidence':evidence,'diagnostics':diagnostics}
    answer=_refine_outer_boundary(frame,mesh,profile,allowed,visible,result,**options)
    if answer is not None or not recover_carrier or not mesh.get('assembly',{}).get('orientation_hole'):return answer
    answer=_refine_outer_boundary(frame,mesh,profile,allowed,visible,result,sample_count=128,partial=True,**options)
    if answer is not None:return answer
    return _refine_outer_boundary(frame,mesh,profile,allowed,visible,result,sample_count=128,partial=True,joint=True,**options)


def _refine_outer_boundary(frame,mesh,profile,allowed,visible,result,*,evidence=None,diagnostics=None,sample_count=64,partial=False,joint=False):
    """Refit a validated carrier to its material/background transitions.

    A small fixed-size model can sit on an inner rim. Find each nearby outer
    boundary independently, reject protruding parts, then apply the existing
    planar fit with a tightly bounded scale correction. This never creates a
    detection from an unvalidated frame or changes the calibration/STL.
    """
    def rejected(reason,**values):
        if diagnostics is not None:diagnostics.update(reason=reason,**values)
        return None
    from .vision import world_xy,project_plane,fit_planar_rectangle,_detect
    from .rectangle_model import quad_visibility
    c=result.get('selected')
    if mesh.get('shape')!='rectangle' or not c or not c.get('shape_match') or not c.get('metric') or c.get('partial_visible'):return None
    quad=np.asarray(c['quad'],float);center=quad.mean(0)
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    gray=evidence.source.gray();white=evidence.source.white()
    # Preserve material/background polarity when the whole carrier is dim.
    # Estimate exposure only inside the independently detected quadrilateral.
    footprint=np.zeros(gray.shape,np.uint8)
    cv2.fillConvexPoly(footprint,np.rint(quad).astype(np.int32),255)
    hsv=evidence.source.hsv()
    neutral=(footprint>0)&(allowed>0)&(visible>0)&(hsv[:,:,1]<60)
    brightness=float(np.percentile(hsv[:,:,2][neutral],90)) if np.any(neutral) else 0.
    if 90<=brightness<180:
        white=np.clip((hsv[:,:,2]*(180/brightness)-90)/90,0,1)*(1-np.clip((hsv[:,:,1].astype(float)-40)/120,0,1))
    offsets=np.linspace(-16,16,129);lines=[];coverage=[];measured=[];observed_along=[]
    required=sample_count*3//8
    for a,b in zip(quad,np.roll(quad,-1,axis=0)):
        along=np.linspace(.08,.92,sample_count);uv=a+(b-a)*along[:,None]
        tangent=b-a;normal=np.array([-tangent[1],tangent[0]]);normal/=max(np.linalg.norm(normal),1e-9)
        if normal@(center-uv.mean(0))<0:normal=-normal
        scan=uv[:,None,:]+offsets[None,:,None]*normal;flat=scan.reshape(-1,2)
        inward=sample(white,flat+normal*2,0).reshape(sample_count,-1)
        outward=sample(white,flat-normal*2,0).reshape(sample_count,-1)
        contrast=(sample(gray,flat+normal*1.5,0)-sample(gray,flat-normal*1.5,0)).reshape(sample_count,-1)/255
        valid=((sample(allowed,flat,0)>254)&(sample(visible,flat,0)>254)).reshape(sample_count,-1)
        score=contrast+.6*(inward-outward);score[(inward<.65)|~valid]=-1
        best=np.argmax(score,axis=1);chosen=offsets[best];keep=(score[np.arange(sample_count),best]>.12)&(abs(chosen)<15.75)
        # A loaded part can hide more than half of one rim. A measured span
        # across at least 35% of that side still constrains its line; below we
        # separately require three nearly complete sides and a valid CAD fit.
        if np.count_nonzero(keep)<required:return rejected('boundary_points',side=len(coverage),observed_points=int(np.count_nonzero(keep)),required_points=required)
        median=np.median(chosen[keep]);mad=np.median(abs(chosen[keep]-median))
        keep &=abs(chosen-median)<=max(1.,3*mad)
        if np.count_nonzero(keep)<required or np.ptp(along[keep])<.35:return rejected('boundary_span',side=len(coverage),observed_points=int(np.count_nonzero(keep)),required_points=required,span=float(np.ptp(along[keep])) if np.any(keep) else 0.,required_span=.35)
        points=scan[np.arange(sample_count),best][keep]
        measured.append(points);observed_along.append(along[keep])
        direction_origin=cv2.fitLine(np.float32(points),cv2.DIST_HUBER,0,.01,.01).ravel()
        direction=direction_origin[:2];origin=direction_origin[2:]
        if abs(direction@(tangent/np.linalg.norm(tangent)))<np.cos(np.deg2rad(3)):return rejected('boundary_direction',side=len(coverage),alignment=float(abs(direction@(tangent/np.linalg.norm(tangent)))),angle_limit_deg=3)
        n=np.array([-direction[1],direction[0]]);lines.append(np.r_[n,-n@origin]);coverage.append(float(keep.mean()))
    strong=(any(coverage[i]>=.85 and coverage[(i+1)%4]>=.85 for i in range(4)) if partial else sorted(coverage)[1]>=.85)
    if not strong:return rejected('boundary_three_sides',coverage=coverage,required_coverage=.85)
    corners=[]
    for i in range(4):
        matrix=np.array([lines[i-1][:2],lines[i][:2]])
        if abs(np.linalg.det(matrix))<.2:return rejected('boundary_parallel_lines',corner=i)
        corners.append(np.linalg.solve(matrix,-np.array([lines[i-1][2],lines[i][2]])))
    z=detection_plane_z(profile,mesh);xy=world_xy(corners,profile,z)
    fit=fit_planar_rectangle(xy,mesh['size_mm'][:2],profile,z)
    if fit is None or abs(fit['scale']-1)>.03 or fit['corner_error_mm']>2:return rejected('boundary_geometry',scale=fit['scale'] if fit else None,corner_error_mm=fit['corner_error_mm'] if fit else None,scale_error_limit=.03,corner_limit_mm=2)
    if np.linalg.norm(fit['center']-c['metric']['center_xy_mm'])>12:return rejected('boundary_center_shift',limit_mm=12)
    extra={}
    if joint:
        from .carrier_boundary import fit_observed
        fit,extra=fit_observed(fit,xy,measured,observed_along,profile,z)
        if fit is None:return rejected(**extra)
    q=project_plane(fit['xy'],profile,z);fraction=quad_visibility(q,allowed,visible)
    if fraction<.9999:return rejected('boundary_visibility',fraction=float(fraction),required_fraction=.9999)
    distance=evidence.distance()
    support=[];costs=[]
    for i,(a,b) in enumerate(zip(q,np.roll(q,-1,axis=0))):
        along=observed_along[i] if joint else np.linspace(.08,.92,64)
        uv=a+(b-a)*along[:,None];observed=sample(visible,uv,0)>254
        if np.count_nonzero(observed)<48:return rejected('boundary_visible_edge',side=len(support),observed_points=int(np.count_nonzero(observed)),required_points=48)
        d=sample(distance,uv)[observed];support.append(float(np.mean(d<2.5)));costs.append(float(np.mean(np.minimum(d,5)/5)))
    # Three fully observed sides plus the independently fitted, partly
    # occluded fourth are required; a missing whole side is never inferred.
    if min(support)<.60 or sorted(support)[2 if partial else 1]<.90 or np.mean(costs)>.20:return rejected('boundary_edge_support',coverage=coverage,edge_support=support,minimum_support=.60,other_sides_support=.90,mean_cost=float(np.mean(costs)),cost_limit=.20)
    proposal={'quad':q,'edge_support':support,'edge_cost':float(np.mean(costs)),'visible_fraction':fraction}
    checked=_detect(frame,mesh,None,profile,allowed_mask=allowed,proposals=[proposal],visible_mask=visible,evidence=evidence)
    chosen=checked.get('selected')
    if not chosen:return rejected('boundary_shape_validation')
    chosen.update(observed_boundary_refined=True,boundary_coverage=coverage,boundary_scale=fit['scale'],boundary_corner_error_mm=fit['corner_error_mm'])
    chosen.update(extra)
    if partial:chosen['recovery_method']='observed_joint' if joint else 'dense_partial'
    chosen['metric']['dimension_source']='observed_boundary_fit'
    chosen['outer_shape']['fit_from_model']=False
    out=deepcopy(result);out['selected']=chosen
    out['candidates']=[chosen if v is c or v['quad']==c['quad'] else v for v in out['candidates']]
    return out


def merge_refined_boundaries(frame,mesh,profile,allowed,visible,result,*,evidence=None,recover_carrier=False):
    """Merge duplicate hypotheses only when every one fits the same observed rim."""
    candidates=result.get('candidates',[])
    if mesh.get('shape')!='rectangle' or result.get('status')!='ambiguous' or not 2<=len(candidates)<=4:return result
    carrier=bool(mesh.get('assembly',{}).get('orientation_hole'))
    if not all(c.get('shape_match') and (c.get('model_recovered') or carrier) for c in candidates):return result
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    refined=[]
    for candidate in candidates:
        single={**result,'selected':candidate,'candidates':[candidate],'status':'shape_match'}
        checked=refine_outer_boundary(frame,mesh,profile,allowed,visible,single,evidence=evidence,recover_carrier=recover_carrier)
        if checked is None:return result
        refined.append(checked['selected'])
    reference=refined[0]
    for candidate in refined[1:]:
        a,b=reference['metric'],candidate['metric']
        angle=abs((a['yaw_deg']-b['yaw_deg']+90)%180-90)
        if np.linalg.norm(np.asarray(a['center_xy_mm'])-b['center_xy_mm'])>1.5 or angle>1:return result
        if max(reference['area_px'],candidate['area_px'])/min(reference['area_px'],candidate['area_px'])>1.02:return result
    chosen=min(refined,key=lambda c:c['edge_cost'])
    return {**result,'selected':chosen,'candidates':[chosen],'status':'shape_match','duplicate_boundary_hypotheses':len(candidates)}


def refine(frame,mesh,profile,allowed,visible,result,*,evidence=None,recover_carrier=False):
    from .vision import world_xy,_detect
    from .rectangle_model import quad_visibility
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    if result.get('status')=='ambiguous':return merge_refined_boundaries(frame,mesh,profile,allowed,visible,result,evidence=evidence,recover_carrier=recover_carrier)
    c=result.get('selected')
    carrier=bool(mesh.get('assembly',{}).get('orientation_hole'))
    if not c or not c.get('metric') or not (c.get('model_recovered') or carrier):return result
    diagnostics={}
    boundary=refine_outer_boundary(frame,mesh,profile,allowed,visible,result,evidence=evidence,diagnostics=diagnostics,recover_carrier=recover_carrier)
    if boundary is not None:return boundary
    # A carrier's fixed-size edge model can lock onto an inner rim. Never
    # publish that alternative pose when the measured outer boundary fails.
    if carrier:
        return {**result,'selected':None,'candidates':[],'status':'not_found','boundary_unverified':True,'boundary_rejection':diagnostics}
    if not c.get('model_recovered'):return result
    ring=mesh.get('shape','ring')=='ring'
    if ring and len(mesh.get('holes',[]))!=1:return result
    z=detection_plane_z(profile,mesh);quad=world_xy(c['quad'],profile,z)
    center=np.asarray(c['metric']['center_xy_mm']);outer=quad-center
    polygons=[outer]
    if ring:
        hole=mesh['holes'][0];hc=np.asarray(hole['center']);hs=np.asarray(hole['size'])/2
        # Follow the same ordered opening coordinates as the detector's warp.
        uv=np.array([hc+[-hs[0],-hs[1]],hc+[hs[0],-hs[1]],hc+hs,hc+[-hs[0],hs[1]]])
        polygons.append(outer[0]+uv[:,0,None]*(outer[1]-outer[0])+uv[:,1,None]*(outer[3]-outer[0]))
    along=np.linspace(.17,.83,32) if ring else np.linspace(.08,.92,32)
    points=np.concatenate([a+(b-a)*along[:,None] for poly in polygons for a,b in zip(poly,np.roll(poly,-1,axis=0))])
    gray=evidence.source.blurred(ring);distance=evidence.distance(enhanced=ring);white=evidence.source.white()
    T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));K=np.asarray(profile['intrinsics']['K'])
    def project(xy):
        camera=np.c_[xy,np.full(len(xy),z)]@T[:3,:3].T+T[:3,3];uv=camera@K.T
        return uv[:,:2]/uv[:,2:]
    def transformed(local,delta):
        a=np.deg2rad(delta[2]);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
        return project(local@R.T+center+delta[:2])
    initial=transformed(points,[0,0,0]);observed=sample(visible,initial,0)>254
    if np.any(observed.reshape(-1,32).sum(1)<24):return result
    def evaluate(delta):
        uv=transformed(points,delta);lines=uv.reshape(-1,32,2)
        tangent=lines[:,-1]-lines[:,0];normal=np.c_[-tangent[:,1],tangent[:,0]]
        normal/=np.maximum(np.linalg.norm(normal,axis=1,keepdims=True),1e-9)
        target=transformed(np.array([[0.,0.]]),delta)[0]
        normal*=np.where(np.sum(normal*(target-lines.mean(1)),axis=1)>=0,1,-1)[:,None]
        # On a ring's opening the material lies away from its centre.
        if ring:normal[4:]*=-1
        normal=np.repeat(normal,32,axis=0)
        d=sample(distance,uv);inside=(sample(visible,uv,0)>254)&(sample(allowed,uv,0)>254)
        inward=sample(white,uv+normal*2,0);outward=sample(white,uv-normal*2,0)
        color=.30*(1-inward)+.20*np.maximum(outward-inward,0)
        residual=np.where(observed,np.where(inside,np.minimum(d,5)/3+color,3),0)
        return residual,uv,d,inside,normal
    start=evaluate([0,0,0])[0]
    fit=least_squares(lambda delta:evaluate(delta)[0],np.zeros(3),bounds=([-2,-2,-1],[2,2,1]),loss='soft_l1',max_nfev=25,ftol=1e-5,xtol=1e-5,gtol=1e-5)
    residual,uv,d,inside,normal=evaluate(fit.x)
    objective=lambda r:float(np.mean(2*(np.sqrt(1+r*r)-1)))
    if not np.isfinite(fit.x).all() or objective(residual)>=objective(start)-1e-5:return result
    if np.any(observed&~inside):return result
    count=observed.reshape(-1,32).sum(1)
    support=(((d<2.5)&observed).reshape(-1,32).sum(1)/count)
    costs=((np.minimum(d,5)/5*observed).reshape(-1,32).sum(1)/count)
    if ring:
        gx,gy=evidence.source.gradient_directions(True)
        regular=min(support[:4])>=.40 and np.mean(support[:4])>=.65
        faint=min(support[:4])>=.25 and sorted(support[:4])[1]>=.85 and np.mean(support[:4])>=.80
        alignment=np.abs(sample(gx,uv,0)*normal[:,0]+sample(gy,uv,0)*normal[:,1])
        costs+=.20*(1-alignment).reshape(-1,32).mean(1)
        cost=float(.65*costs[:4].mean()+.35*costs[4:].mean())
        if not (regular or faint) or sorted(support[4:])[1]<.55 or cost>.18:return result
    else:
        cost=float(costs.mean())
        if min(support)<.65 or np.mean(support)<.8 or cost>.20:return result
    q=transformed(outer,fit.x).astype(np.float32);fraction=quad_visibility(q,allowed,visible)
    if fraction<(.9999 if ring else .90):return result
    proposal={'quad':q,'edge_support':support.tolist(),'edge_cost':cost,'visible_fraction':fraction}
    checked=_detect(frame,mesh,None,profile,allowed_mask=allowed,proposals=[proposal],visible_mask=visible,evidence=evidence)
    if not checked.get('selected'):return result
    out=deepcopy(result);chosen=checked['selected']
    chosen.update(subpixel_refined=True,refinement_delta_mm_deg=fit.x.tolist(),refinement_objective_before=objective(start),refinement_objective_after=objective(residual))
    out['selected']=chosen
    out['candidates']=[chosen if v is c or v['quad']==c['quad'] else v for v in out['candidates']]
    return out
