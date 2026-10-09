"""STL-informed pallet inspection. Results are observations, never motion goals."""
import hashlib
from functools import lru_cache
from pathlib import Path
import cv2
import numpy as np
from .domain import ROOT
from .height_reference import detection_plane_z

def stl_profile(path=ROOT/'assets/jigs/pallet.stl',unit='mm'):
    raw=Path(path).read_bytes()
    import re
    count=int.from_bytes(raw[80:84],'little') if len(raw)>=84 else 0
    if count and len(raw)==84+count*50:
        tri=np.frombuffer(raw[84:],dtype=np.dtype([('normal','<f4',3),('v','<f4',(3,3)),('attr','<u2')]))['v'].astype(float)
    else:
        vertices=re.findall(r'\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)',raw.decode('ascii',errors='strict'))
        if not vertices or len(vertices)%3:raise ValueError('STL 삼각형 형식을 확인하세요.')
        tri=np.asarray(vertices,float).reshape(-1,3,3)
    if unit not in ('mm','cm','m'):raise ValueError('STL 단위를 선택하세요.')
    tri*=dict(mm=1,cm=10,m=1000)[unit]
    if not np.isfinite(tri).all():raise ValueError('STL 좌표 오류')
    low=tri.min((0,1));high=tri.max((0,1));extent=high-low
    if np.any(extent<=0):raise ValueError('STL 크기 오류')
    # A fixed assembly has tall, narrow fixtures above the low carrier rim.
    # Derive detection geometry from its enclosing carrier shell, not from
    # half the total assembly height; keep the full STL size and path for UI.
    from .assembly_model import carrier_components
    assembly=carrier_components(tri)
    if assembly is not None:
        tri,assembly_info=assembly
    base_low=tri.min((0,1));base_extent=tri.max((0,1))-base_low
    flat=np.ptp(tri[:,:,2],axis=1)<.001
    levels=sorted(set(np.round(tri[flat,0,2],3)),reverse=True)
    rim=None
    for z in levels:
        faces=tri[flat & (abs(tri[:,0,2]-z)<.001)]
        area=np.linalg.norm(np.cross(faces[:,1]-faces[:,0],faces[:,2]-faces[:,0]),axis=1).sum()/2
        if z>base_low[2]+base_extent[2]/2 and area>extent[0]*extent[1]*.2:rim=z;break
    if rim is None:raise ValueError('STL의 넓은 외곽 윗면을 찾지 못했습니다.')
    faces=tri[np.min(tri[:,:,2],axis=1)>=rim-.001]
    mask=np.zeros((320,320),np.uint8)
    for t in faces:
        points=np.rint(10+(t[:,:2]-low[:2])/extent[:2]*300).astype(np.int32);cv2.fillConvexPoly(mask,points,255)
    contours,hierarchy=cv2.findContours(mask,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    holes=[]
    if hierarchy is not None:
        for c,h in zip(contours,hierarchy[0]):
            if h[3]>=0 and cv2.contourArea(c)>100:
                x,y,w,h=cv2.boundingRect(c);holes.append({'center':[(x+w/2-10)/300,(y+h/2-10)/300],'size':[w/300,h/300]})
    extra={'assembly':assembly_info} if assembly is not None else {}
    if assembly is None:
        from .raised_model import ridge_edges
        ridges=ridge_edges(tri,rim)
        if ridges:extra['raised_edges_mm']=ridges
    return {**extra,'sha256':hashlib.sha256(raw if unit=='mm' else raw+unit.encode()).hexdigest(),'stl_path':str(Path(path).resolve()),'unit':unit,'low_mm':low.tolist(),'size_mm':extent.tolist(),'rim_z_mm':float(rim),'holes':holes}

def world_xy(points,profile,z_mm):
    K=np.array(profile['intrinsics']['K']);D=np.array(profile['intrinsics']['D']);T=np.array(profile['extrinsics']['base_from_camera'])
    uv=cv2.undistortPoints(np.asarray(points,np.float64).reshape(-1,1,2),K,D).reshape(-1,2)
    rays=np.c_[uv,np.ones(len(uv))]@T[:3,:3].T;origin=T[:3,3]
    # Imported extrinsic translations are millimetres (explicit profile provenance).
    if np.any(abs(rays[:,2])<1e-8):raise ValueError('카메라 광선이 검출면과 평행합니다.')
    distance=(z_mm-origin[2])/rays[:,2]
    if np.any(distance<=0):raise ValueError('검출면이 카메라 뒤에 있습니다.')
    return (origin+distance[:,None]*rays)[:,:2]

def ordered_quad(p):
    p=np.array(p,dtype=np.float32).reshape(4,2);center=p.mean(0)
    p=p[np.argsort(np.arctan2(p[:,1]-center[1],p[:,0]-center[0]))]
    return np.roll(p,-int(np.argmin(p.sum(1))),axis=0)

def rectangle_quality(xy,size_mm):
    """Compare each STL dimension and aspect ratio on the physical plane."""
    q=np.asarray(xy,float);size=np.asarray(size_mm,float)
    if q.shape!=(4,2) or size.shape!=(2,) or not np.isfinite(q).all() or not np.isfinite(size).all() or np.any(size<=0):
        return {'valid':False,'score':0.}
    edges=np.roll(q,-1,axis=0)-q;sides=np.linalg.norm(edges,axis=1)
    if sides.min()<=0:return {'valid':False,'score':0.}
    expected=np.array([size,size[::-1]])[:,[0,1,0,1]]
    errors=np.max(abs(sides-expected)/expected,axis=1);orientation=int(np.argmin(errors))
    scale_error=float(errors[orientation])
    observed=np.array([(sides[0]+sides[2])/2,(sides[1]+sides[3])/2])
    target_ratio=float(size.max()/size.min());observed_ratio=float(observed.max()/observed.min())
    ratio_error=abs(observed_ratio/target_ratio-1)
    opposite_error=float(max(abs(sides[0]-sides[2])/min(sides[0],sides[2]),abs(sides[1]-sides[3])/min(sides[1],sides[3])))
    cosines=np.sum(edges*np.roll(edges,-1,axis=0),axis=1)/(sides*np.roll(sides,-1))
    angle_error=float(np.degrees(np.arcsin(np.clip(abs(cosines).max(),0,1))))
    valid=scale_error<=.22 and ratio_error<=.15 and opposite_error<=.22 and angle_error<=15
    score=max(0.,1-max(scale_error/.22,ratio_error/.15,opposite_error/.22,angle_error/15))
    return {'valid':bool(valid),'score':score,'size_error_ratio':scale_error,'aspect_error_ratio':ratio_error,
            'stl_aspect_ratio':target_ratio,'observed_aspect_ratio':observed_ratio,'opposite_side_error_ratio':opposite_error,
            'right_angle_error_deg':angle_error,'width_edge_index':orientation}

def fit_planar_rectangle(xy,size_mm,profile,z_mm):
    """Fit all four corners on the known near-horizontal rim plane.

    Retain a small uniform scale freedom for ±10 mm support-height uncertainty
    and pixel/calibration error. Never infer a new 3-D tilt from a partial rim.
    """
    quality=rectangle_quality(xy,size_mm)
    if not quality['valid']:return None
    q=np.asarray(xy,float)
    if quality['width_edge_index']:q=np.roll(q,-1,axis=0)
    w,h=map(float,size_mm)
    model=np.array([[-w,-h],[w,-h],[w,h],[-w,h]])/2
    # world_xy may reverse image winding when the camera points downward.
    e1=q[1]-q[0];e2=q[2]-q[1]
    if e1[0]*e2[1]-e1[1]*e2[0]<0:model[:,1]*=-1
    center=q.mean(axis=0);target=q-center
    U,_,Vt=np.linalg.svd(model.T@target);R=U@Vt
    if np.linalg.det(R)<0:return None
    rotated=model@R
    scale=float(np.sum(rotated*target)/np.sum(model*model))
    fitted=center+scale*rotated
    residual=float(np.linalg.norm(fitted-q,axis=1).max())
    camera_z=float(profile['extrinsics']['base_from_camera'][2][3])
    height_scale=10./max(abs(camera_z-z_mm),1.)
    scale_tolerance=min(.22,.05+height_scale)
    if (abs(scale-1)>scale_tolerance or residual>max(3.,min(w,h)*.065) or
        quality['right_angle_error_deg']>8 or quality['aspect_error_ratio']>.10 or
        quality['opposite_side_error_ratio']>.18):return None
    return {'xy':fitted,'center':center,'quality':quality,'scale':scale,
            'corner_error_mm':residual,'height_tolerance_mm':10.}

def project_plane(points,profile,z_mm):
    xy=np.asarray(points,float);world=np.c_[xy,np.full(len(xy),z_mm)]
    T=np.linalg.inv(np.array(profile['extrinsics']['base_from_camera']))
    camera=world@T[:3,:3].T+T[:3,3]
    uv,_=cv2.projectPoints(camera,np.zeros(3),np.zeros(3),np.array(profile['intrinsics']['K']),np.array(profile['intrinsics']['D']))
    return uv.reshape(-1,2).astype(np.float32)

def detect(frame,mesh,roi=None,profile=None,*,prepared=None):
    from .lens_geometry import DetectionFrame,detection_region
    prepared=prepared or DetectionFrame(frame,profile)
    if mesh.get('shape')=='rectangle':prepared=detection_region(prepared,roi)
    if prepared.error:return {'selected':None,'candidates':[],'status':'calibration_mismatch','error':prepared.error,'verified_for_motion':False}
    # A shared lens image can still use each jig's own support-plane height.
    from copy import deepcopy
    geometry=deepcopy(prepared.profile)
    if geometry is not None and profile is not None:geometry['table_z_mm']=profile.get('table_z_mm',-7.4)
    allowed=prepared.mask(roi)
    from .frame_evidence import FrameEvidence,RegionEvidence
    if prepared.evidence is None:prepared.evidence=FrameEvidence(prepared.frame)
    evidence=RegionEvidence(prepared.frame,allowed,prepared.evidence)
    use_grid=mesh.get('method')=='grid'
    hints=[]
    if use_grid:
        from .grid_model import grid_hypotheses
        hints=grid_hypotheses(prepared.frame,mesh,allowed,geometry,evidence=evidence,visible=prepared.mask(None))
    def finish(result):
        from .pose_refinement import refine
        result=refine(prepared.frame,mesh,geometry,allowed,prepared.mask(None),result,evidence=evidence)
        if use_grid:
            from .grid_model import attach_grid
            result=attach_grid(result,hints,mesh)
            if result.get('grid_seed_recovery'):
                valid=[c for c in result['candidates'] if c.get('grid_verified')]
                result.update(candidates=valid,selected=valid[0] if len(valid)==1 else None,status='shape_match' if len(valid)==1 else 'ambiguous' if valid else 'not_found')
            result['grid_direction_period_deg']=180
        if mesh.get('assembly',{}).get('orientation_hole'):
            from .orientation_model import orient_carrier
            result=orient_carrier(prepared.frame,mesh,allowed,geometry,result,evidence=evidence)
        if mesh.get('shape','ring')=='ring':
            from .raised_model import attach_raised
            result=attach_raised(prepared.frame,mesh,allowed,geometry,result,evidence=evidence)
        return prepared.restore(result)
    def recover_grid():
        nonlocal hints
        from .rectangle_model import rectangle_proposals
        from .grid_model import matching_grid
        for refit in (False,True):
            if refit:
                # A noisy longest divider can bias its distant crossings.
                # Retry observed-fragment fitting only after normal evidence
                # fails, retaining every size, ROI and grid acceptance check.
                hints=grid_hypotheses(prepared.frame,mesh,allowed,geometry,evidence=evidence,visible=prepared.mask(None),refine_lines=True)
            if not hints:continue
            proposals=rectangle_proposals(prepared.frame,mesh,allowed,geometry,prepared.mask(None),seed_poses=[h['pose'] for h in hints],evidence=evidence)
            if not proposals:continue
            recovered=_detect(prepared.frame,mesh,None,geometry,allowed_mask=allowed,proposals=proposals,visible_mask=prepared.mask(None),evidence=evidence)
            recovered['candidates']=[c for c in recovered['candidates'] if c['shape_match'] and matching_grid(c,hints,mesh)]
            valid=recovered['candidates']
            if valid:
                recovered.update(selected=valid[0] if len(valid)==1 else None,status='shape_match' if len(valid)==1 else 'ambiguous',grid_seed_recovery=True)
                finished=finish(recovered)
                if finished['status']!='not_found':return finished
        return None
    # Grid-verified assemblies already have independent pose seeds. Avoid
    # first exhausting the unrelated contour search on every loaded frame.
    grid_tried=bool(use_grid and mesh.get('assembly'))
    if grid_tried:
        recovered=recover_grid()
        if recovered is not None:return recovered
    # A loaded carrier can expose a smaller rectangular contour on alternate
    # frames. Prefer the independently supported, fixed-size outer rim so the
    # pose does not jump between that contour and the physical carrier.
    if mesh.get('shape')=='rectangle':
        from .rectangle_model import rectangle_proposals
        visible=prepared.mask(None)
        proposals=rectangle_proposals(prepared.frame,mesh,allowed,geometry,visible,evidence=evidence)
        if proposals:
            result=_detect(prepared.frame,mesh,None,geometry,allowed_mask=allowed,proposals=proposals,visible_mask=visible,evidence=evidence)
            if result['status']!='not_found':
                from .pose_refinement import refine
                result.update(edge_model_recovery=True,edge_gap_repair=True)
                return finish(result)
    result=_detect(prepared.frame,mesh,None,geometry,allowed_mask=allowed,evidence=evidence)
    if result['status']=='not_found' and not result['candidates']:
        result=_detect(prepared.frame,mesh,None,geometry,close_gaps=True,allowed_mask=allowed,evidence=evidence)
        if result['status']=='not_found' and not result['candidates']:
            result=_detect(prepared.frame,mesh,None,geometry,close_gaps=True,low_contrast=True,allowed_mask=allowed,evidence=evidence)
    if result['status']=='not_found' and not result['candidates']:
        from .edge_model import ring_proposals
        proposals=ring_proposals(prepared.frame,mesh,allowed,geometry,evidence=evidence)
        if proposals:
            result=_detect(prepared.frame,mesh,None,geometry,allowed_mask=allowed,proposals=proposals,evidence=evidence)
            result.update(edge_model_recovery=True,edge_gap_repair=True,low_contrast_repair=True)
    if use_grid and not grid_tried and result['status']=='not_found':
        recovered=recover_grid()
        if recovered is not None:return recovered

    return finish(result)

def supported_outer_quad(contour,edges):
    """Repair small inward contour detours only when all four rims are visible."""
    hull=cv2.convexHull(contour);area=cv2.contourArea(hull)
    if area<=0 or cv2.contourArea(contour)/area<.90:return None
    poly=cv2.approxPolyDP(hull,.02*cv2.arcLength(hull,True),True)
    if len(poly)!=4:return None
    q=ordered_quad(poly)
    distance=cv2.distanceTransform(cv2.bitwise_not(edges),cv2.DIST_L2,3)
    for a,b in zip(q,np.roll(q,-1,axis=0)):
        points=np.rint(a+(b-a)*np.linspace(.08,.92,100)[:,None]).astype(int)
        if np.mean(distance[points[:,1],points[:,0]]<3)<.75:return None
    return poly

def quad_inside_mask(q,mask):
    # A freehand region may be concave. Its interior must contain the whole
    # object, not just its corners or outer border.
    points=np.rint(q).astype(np.int32);h,w=mask.shape
    if np.any(points<0) or np.any(points[:,0]>=w) or np.any(points[:,1]>=h):return False
    x0,y0=points.min(0);x1,y1=points.max(0)+1
    footprint=np.zeros((y1-y0,x1-x0),np.uint8)
    cv2.fillConvexPoly(footprint,points-[x0,y0],255)
    return not np.any((footprint>0)&(mask[y0:y1,x0:x1]==0))

def _detect(frame,mesh,roi=None,profile=None,*,close_gaps=False,low_contrast=False,allowed_mask=None,proposals=None,visible_mask=None,evidence=None):
    h,w=frame.shape[:2]
    if profile and profile.get('intrinsics') and list(profile['intrinsics']['size'])!=[w,h]:return {'selected':None,'candidates':[],'status':'calibration_mismatch','error':'현재 영상 해상도와 카메라 보정 해상도가 다릅니다. 설정에서 보정을 확인하세요.','verified_for_motion':False}
    from .frame_evidence import FrameEvidence
    source=evidence.source if evidence is not None else FrameEvidence(frame)
    gray=source.blurred()
    edges=source.edges(20 if low_contrast else 35,60 if low_contrast else 100)
    method=mesh.get('method','edges')
    if method not in ('edges','combined','grid'):raise ValueError('검출 방식은 윤곽 중심, 색상+윤곽 또는 외곽+교차점을 선택하세요.')
    if method=='combined':
        hsv=source.hsv();white=cv2.inRange(hsv,np.array([0,0,150]),np.array([179,85,255]))
        boundary=cv2.morphologyEx(white,cv2.MORPH_GRADIENT,np.ones((3,3),np.uint8))
        edges=cv2.bitwise_or(edges,boundary)
    from .roi_geometry import roi_mask
    allowed=roi_mask(roi,w,h);x0,y0,x1,y1=0,0,w,h
    if allowed_mask is not None:allowed=cv2.bitwise_and(allowed,allowed_mask)
    edges=cv2.bitwise_and(edges,allowed)
    if close_gaps:
        gap=5 if low_contrast else 3
        edges=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((gap,gap),np.uint8))
        edges=cv2.bitwise_and(edges,allowed)
    contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
    if proposals:contours=[p['quad'].astype(np.float32).reshape(-1,1,2) for p in proposals]
    def within_region(quad):
        if proposals and mesh.get('shape')=='rectangle' and visible_mask is not None:
            from .rectangle_model import quad_visibility
            return quad_visibility(quad,allowed,visible_mask)>=.90
        return quad_inside_mask(quad,allowed)
    candidates=[]
    for contour_index,c in enumerate(contours):
        area=cv2.contourArea(c)
        if not 900<area<(profile or {}).get('_detection_image_area',w*h)*.3:continue
        poly=cv2.approxPolyDP(c,.025*cv2.arcLength(c,True),True)
        if len(poly)!=4 or not cv2.isContourConvex(poly):
            if not low_contrast or mesh.get('shape')!='rectangle':continue
            poly=supported_outer_quad(c,edges)
            if poly is None:continue
        q=ordered_quad(poly);lengths=np.linalg.norm(q-np.roll(q,1,axis=0),axis=1)
        if lengths.min()<20:continue
        if not within_region(q):continue
        H=cv2.getPerspectiveTransform(q,np.float32([[0,0],[255,0],[255,255],[0,255]]))
        center=cv2.perspectiveTransform(np.float32([[[127.5,127.5]]]),np.linalg.inv(H))[0,0]
        metric=None;size_score=0.;outer=None
        symmetry=90 if max(mesh['size_mm'][:2])/min(mesh['size_mm'][:2])<1.01 else 180
        if profile and profile.get('intrinsics') and profile.get('extrinsics'):
            if list(profile['intrinsics']['size'])!=[w,h]:continue
            try:xy=world_xy(q,profile,detection_plane_z(profile,mesh));centerxy=world_xy([center],profile,detection_plane_z(profile,mesh))[0]
            except (ValueError,KeyError):continue
            fitted=fit_planar_rectangle(xy,mesh['size_mm'][:2],profile,detection_plane_z(profile,mesh))
            if fitted is None:continue
            outer=fitted['quality'];outer.update(fit_corner_error_mm=fitted['corner_error_mm'],fit_scale=fitted['scale'],height_tolerance_mm=10.)
            # Keep observed side lengths for diagnostics; display and calculate
            # from the same four-side fit, never from just one damaged edge.
            sides=np.linalg.norm(xy-np.roll(xy,1,axis=0),axis=1)
            # Fit and centre use the same lens-corrected physical plane.
            xy=fitted['xy'];centerxy=fitted['center']
            q=project_plane(xy,profile,detection_plane_z(profile,mesh))
            if not within_region(q):continue
            center=project_plane([centerxy],profile,detection_plane_z(profile,mesh))[0]
            H=cv2.getPerspectiveTransform(q,np.float32([[0,0],[255,0],[255,255],[0,255]]))
            edge=xy[1]-xy[0];yaw=float(np.degrees(np.arctan2(edge[1],edge[0]))%symmetry)
            metric={'center_xy_mm':centerxy.tolist(),'yaw_deg':yaw,'symmetry_deg':symmetry,'sides_mm':sides.tolist(),'verified':False}
            size_score=1-outer['size_error_ratio']
        # Hole must occur at the STL's central opening after perspective correction.
        if mesh.get('shape','ring')=='ring':
            warped=cv2.warpPerspective(gray,H,(256,256))
            inner_edges=cv2.Canny(warped,10,30)
            distance=cv2.distanceTransform(cv2.bitwise_not(inner_edges),cv2.DIST_L2,3)
        matched=[]
        # Loaded carriers have variable contents; never rank them by the
        # appearance of an opening underneath a part or fixture.
        for hole in mesh['holes'] if mesh.get('shape','ring')=='ring' else []:
            cx,cy=np.array(hole['center'])*255;hw,hh=np.array(hole['size'])*255/2
            # Rounded corners are excluded; require evidence on three distinct sides.
            along=np.linspace(-.65,.65,50)
            sides=[np.c_[cx+hw*along,np.full(50,cy-hh)],np.c_[cx+hw*along,np.full(50,cy+hh)],
                   np.c_[np.full(50,cx-hw),cy+hh*along],np.c_[np.full(50,cx+hw),cy+hh*along]]
            support=[]
            for side in sides:
                points=np.clip(np.rint(side).astype(int),0,255)
                support.append(float(np.mean(distance[points[:,1],points[:,0]]<6)))
            matched.append(float(np.mean(support)) if sum(x>.45 for x in support)>=3 else 0.)
        if mesh.get('shape','ring')=='ring' and not mesh['holes']:
            inner_contours,_=cv2.findContours(inner_edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
            matched=[1.] if any(256*256*.04<cv2.contourArea(c)<256*256*.7 and min(cv2.boundingRect(c)[:2])>12 and cv2.boundingRect(c)[0]+cv2.boundingRect(c)[2]<244 and cv2.boundingRect(c)[1]+cv2.boundingRect(c)[3]<244 for c in inner_contours) else []
        # Model recovery verifies the opening in the detection image itself;
        # enlarging a small opening to 256 px can erase its weak bottom edge.
        if proposals and mesh.get('shape','ring')=='ring':
            support=proposals[contour_index]['edge_support'][4:]
            matched=[float(np.mean(support)) if sum(value>=.55 for value in support)>=3 else 0.]
        holes_ok=(bool(matched) and min(matched)>.55) if mesh.get('shape','ring')=='ring' else outer is not None and outer['valid']
        score=(min(matched) if matched else 0)*.45+size_score*.35+(outer['score'] if outer else 0)*.20
        if proposals:score*=1-proposals[contour_index]['edge_cost']
        item={'quad':q.tolist(),'center_px':center.tolist(),'symmetry_deg':symmetry,'image_angle_deg':float(np.degrees(np.arctan2(*(q[1]-q[0])[::-1]))%symmetry),
              'metric':metric,'outer_shape':outer,'hole_matches':matched,'shape_match':holes_ok,'score':score,'area_px':area}
        if proposals and metric:metric['dimension_source']='stl_model'
        if proposals and outer:outer['fit_from_model']=True
        if proposals:item.update(edge_support=proposals[contour_index]['edge_support'],edge_cost=proposals[contour_index]['edge_cost'],model_recovered=True)
        if proposals and proposals[contour_index].get('raised_edge_recovery'):item['raised_edge_recovery']=True
        if proposals and 'visible_fraction' in proposals[contour_index]:
            fraction=proposals[contour_index]['visible_fraction'];item.update(visible_fraction=fraction,partial_visible=fraction<.9999)
        if metric:item['axes_px']=projected_jig_axes(item,profile,detection_plane_z(profile,mesh))
        duplicate=next((i for i,v in enumerate(candidates) if np.linalg.norm(np.array(v['center_px'])-center)<8 and abs(v['area_px']-area)/area<.12),None)
        if duplicate is None:candidates.append(item)
        elif score>candidates[duplicate]['score']:candidates[duplicate]=item
    candidates.sort(key=lambda d:d['score'],reverse=True)
    valid=[v for v in candidates if v['shape_match']]
    return {'candidates':candidates,'selected':valid[0] if len(valid)==1 else None,
            'status':'shape_match' if len(valid)==1 else 'ambiguous' if len(valid)>1 else 'not_found','verified_for_motion':False,
            'edge_gap_repair':close_gaps,'low_contrast_repair':low_contrast}

def draw_outline(image,points,color,*,dashed=False):
    points=np.asarray(points,float)
    if not dashed:
        cv2.polylines(image,[np.rint(points).astype(np.int32)],True,color,2);return
    phase=0.
    for a,b in zip(points,np.roll(points,-1,axis=0)):
        length=float(np.linalg.norm(b-a));offset=0.
        if length<1e-9:continue
        while offset<length-1e-9:
            remaining=(10-phase) if phase<10 else (16-phase)
            step=min(remaining,length-offset)
            if phase<10:
                p=a+(b-a)*offset/length;q=a+(b-a)*(offset+step)/length
                cv2.line(image,tuple(np.rint(p).astype(int)),tuple(np.rint(q).astype(int)),color,2,cv2.LINE_AA)
            offset+=step;phase=(phase+step)%16

def annotate(frame,result,*,jig_color=None,legend=True,layout=None):
    image=frame.copy()
    candidates=list(result['candidates'][:8])
    from .label_layout import LabelLayout
    if layout is None:layout=LabelLayout(image.shape,[c.get('outline_px',c['quad']) for c in candidates])
    chosen=result.get('selected')
    if chosen and chosen.get('quad') and not any(c['quad']==chosen['quad'] for c in candidates):candidates.append(chosen)
    for item in candidates:
        selected=result.get('selected') is not None and item['quad']==result['selected']['quad'];color=jig_color if jig_color is not None else (170,190,40) if selected and not item.get('temporal_match') else (30,165,240)
        q=np.asarray(item.get('outline_px',item['quad']))
        draw_outline(image,q,color,dashed=bool(result.get('live_view') and not result.get('adopted_view')))
        if selected and result.get('live_view') and not result.get('adopted_view') and item.get('grid_verified'):
            for point in item['grid_crossings_px']:cv2.circle(image,tuple(np.rint(point).astype(int)),3,color,1,cv2.LINE_AA)
        if selected and result.get('live_view') and not result.get('adopted_view') and item.get('orientation_verified'):
            cv2.polylines(image,[np.rint(item['orientation_hole_outline_px']).astype(np.int32)],True,color,1,cv2.LINE_AA)
        if selected and not result.get('outline_only'):
            x,y=np.rint(item['center_px']).astype(int)
            axes=item.get('axes_px')
            if axes is None:
                a=np.deg2rad(item['image_angle_deg']);u=np.array([np.cos(a),np.sin(a)]);v=np.array([np.sin(a),-np.cos(a)]);c=np.array([x,y])
                axes=[c,c+28*u,c-16*u,c+28*v,c-16*v]
            center,xp,xn,yp,yn=np.asarray(axes,float)
            strokes=[]
            for positive,negative in [(xp,xn),(yp,yn)]:
                direction=positive-negative;length=np.linalg.norm(direction)
                if length<1e-6:continue
                offset=direction/length*16
                ends=[tuple(np.rint(p).astype(int)) for p in (center-offset,center+offset)]
                strokes.append(ends)
            # Draw the whole outline first so neither stroke cuts across the fill.
            for ends in strokes:cv2.line(image,*ends,(25,25,25),4,cv2.LINE_AA)
            for ends in strokes:cv2.line(image,*ends,color,2,cv2.LINE_AA)
            metric=item.get('metric')
            if metric:
                from .display_coordinates import display_position,display_heading
                dx,dy=display_position(metric['center_xy_mm'])
                heading=display_heading(metric['yaw_deg'],metric.get('symmetry_deg',90))
                labels=[f'X {dx:.1f}  Y {dy:.1f} mm',f'Angle {heading:.1f} deg']
            else:
                labels=[f'({x}, {y}) px',f"Image angle {item['image_angle_deg']:.1f} deg"]
            # Keep labels inside the image, including near its right/top edges.
            width=max(cv2.getTextSize(t,cv2.FONT_HERSHEY_SIMPLEX,.55,1)[0][0] for t in labels)
            position=layout.place(width+8,48,(x,y))
            if position is None:continue
            tx,ty=position;tx+=4;ty+=18
            for i,label in enumerate(labels):
                pos=(tx,ty+i*22)
                cv2.putText(image,label,pos,cv2.FONT_HERSHEY_SIMPLEX,.55,(25,25,25),3,cv2.LINE_AA)
                cv2.putText(image,label,pos,cv2.FONT_HERSHEY_SIMPLEX,.55,color,1,cv2.LINE_AA)
    if not legend:return image
    # Fixed screen-direction legend. The central + still follows jig orientation.
    origin=(38,90)
    for end in ((103,90),(38,25)):
        cv2.arrowedLine(image,origin,end,(25,25,25),4,cv2.LINE_AA,tipLength=.15)
        cv2.arrowedLine(image,origin,end,(170,220,210),2,cv2.LINE_AA,tipLength=.15)
    for label,point in [('X+',(108,96)),('Y+',(45,30))]:
        cv2.putText(image,label,point,cv2.FONT_HERSHEY_SIMPLEX,.55,(25,25,25),4,cv2.LINE_AA)
        cv2.putText(image,label,point,cv2.FONT_HERSHEY_SIMPLEX,.55,(170,220,210),1,cv2.LINE_AA)
    return image

def annotate_all(frame,results,catalog,*,layout=None):
    """Name each current match; missing/ambiguous objects stay in the status list."""
    from .label_layout import LabelLayout
    if layout is None:layout=LabelLayout(frame.shape,[r['selected'].get('outline_px',r['selected']['quad']) for r in results.values() if r.get('selected')])
    image=annotate(frame,{'selected':None,'candidates':[]},layout=layout)
    palette=[(170,190,40),(45,165,245),(225,155,180),(220,175,75),(105,170,235)]
    labels=[]
    for index,(key,config) in enumerate(catalog.items()):
        result=results.get(key,{})
        chosen=result.get('selected')
        if not chosen:continue
        color=palette[index%len(palette)]
        image=annotate(image,{**result,'candidates':[chosen]},jig_color=color,legend=False,layout=layout)
        if not result.get('outline_only'):
            q=np.asarray(chosen['quad']);labels.append((f"{index+1} · {config['name']}",q.min(0),color))
    from PIL import Image,ImageDraw
    rgb=Image.fromarray(cv2.cvtColor(image,cv2.COLOR_BGR2RGB));draw=ImageDraw.Draw(rgb);font=roi_font()
    for text,corner,color in labels:
        if not font:text=text.split(' · ')[0]
        while len(text)>1 and draw.textbbox((0,0),text,font=font)[2]>image.shape[1]-20:text=text[:-2]+'…'
        box=draw.textbbox((0,0),text,font=font);width=box[2]-box[0];height=box[3]-box[1]
        position=layout.place(width+8,height+8,corner)
        if position is None:continue
        x,y=position
        draw.rounded_rectangle((x,y,x+width+8,y+height+8),radius=3,fill=tuple(reversed(color)))
        draw.text((x+4,y+4-box[1]),text,font=font,fill='#10252F')
    return cv2.cvtColor(np.asarray(rgb),cv2.COLOR_RGB2BGR)

@lru_cache(maxsize=1)
def roi_font():
    from PIL import ImageFont
    for path in ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc','/usr/share/fonts/truetype/nanum/NanumGothic.ttf'):
        if Path(path).exists():return ImageFont.truetype(path,16)
    return None

def annotate_roi(frame,roi):
    """One rendered ROI shared by the live view and saved capture."""
    image=frame.copy()
    if roi is None:return image
    from PIL import Image,ImageDraw
    from .roi_geometry import roi_vertices
    h,w=image.shape[:2];points=np.rint(np.asarray(roi_vertices(roi))*[w,h]).astype(np.int32)
    x1,y1=points.min(axis=0)
    cv2.polylines(image,[points],True,(189,211,53),3,cv2.LINE_AA)
    rgb=Image.fromarray(cv2.cvtColor(image,cv2.COLOR_BGR2RGB));draw=ImageDraw.Draw(rgb)
    font=roi_font()
    draw.text((x1+8,y1+8),'감지 범위' if font else 'ROI',font=font,fill='#35d3bd',stroke_width=1,stroke_fill='#192020')
    return cv2.cvtColor(np.asarray(rgb),cv2.COLOR_RGB2BGR)

from .jig_consensus import StableCandidate,checked_seconds,HOLD_RANGE

from copy import deepcopy

class PoseLatch:
    """Bounded display hold, retaining measurement age separately from video age."""
    def __init__(self,seconds=10.,acquisition_seconds=3.,attempts_limit=3):
        import threading
        self.lock=threading.Lock();self.seconds=checked_seconds(seconds,HOLD_RANGE,'실행 기준 유지 시간');self.saved=None;self.measured_at=None;self.frozen=False;self.stability=StableCandidate(acquisition_seconds,attempts_limit);self.last_output=None;self.next_saved=None
    def clear(self,started=None):
        with self.lock:
            if self.frozen:return False
            self.saved=None;self.measured_at=None;self.last_output=None;self.next_saved=None;self.stability.clear(started);return True
    def freeze(self,enabled):
        with self.lock:
            if self.frozen and not enabled:self.stability.clear();self.next_saved=None
            self.frozen=bool(enabled)
    def configure(self,seconds,*,acquisition_seconds=None,attempts_limit=None):
        seconds=checked_seconds(seconds,HOLD_RANGE,'실행 기준 유지 시간')
        with self.lock:
            if self.frozen:raise ValueError('실행 중에는 지그 위치 유지 설정을 변경할 수 없습니다.')
            if acquisition_seconds is not None or attempts_limit is not None:self.stability.configure(self.stability.seconds if acquisition_seconds is None else acquisition_seconds,attempts_limit)
            self.seconds=checked_seconds(seconds,HOLD_RANGE,'실행 기준 유지 시간');self.saved=None;self.measured_at=None;self.last_output=None;self.next_saved=None;self.stability.clear()
    def _output(self,result,now):
        measured=result.get('pose_measured_at',now)
        if result.get('selected') and not self.frozen and (measured is None or not 0<=now-measured<self.seconds):
            result={**result,'selected':None,'status':'acquisition_failed','acquisition_issue':'측정 유효시간 만료'}
        if not self.frozen and result.get('selected'):self.next_saved=deepcopy(result)
        held=self.saved is not None and (self.frozen or now-self.measured_at<self.seconds)
        if not held:
            pending=self.next_saved;self.next_saved=None
            stamp=pending.get('pose_measured_at',now) if pending else None
            self.saved=pending if stamp is not None and 0<=now-stamp<self.seconds else None
            self.measured_at=stamp if self.saved else None
        output=deepcopy(self.saved if self.saved is not None else result)
        output.update(pose_held=held,pose_frozen=self.frozen,pose_measured_at=self.measured_at,
                      hold_remaining_s=max(0.,self.seconds-(now-self.measured_at)) if self.saved else 0.,verified_for_motion=False)
        self.last_output=output
        return deepcopy(output)
    def update(self,result,now,*,profile=None,mesh=None):
        with self.lock:
            if self.frozen and self.saved is None:return {'candidates':[],'selected':None,'status':'not_measured','pose_held':True,'pose_frozen':True,'verified_for_motion':False,'hold_remaining_s':0}
            if not self.frozen:result=self.stability.update(result,now,profile=profile,mesh=mesh)
            return self._output(result,now)
    def poll(self,now):
        with self.lock:
            if self.frozen:return deepcopy(self.last_output)
            result=self.stability.finish(now)
            if result is not None:return self._output(result,now)
            if self.saved is not None and now-self.measured_at>=self.seconds:
                return self._output({'selected':None,'candidates':[],'status':'not_measured'},now)
            return deepcopy(self.last_output)

def projected_jig_axes(item,profile,z_mm):
    metric=item['metric'];cx,cy=metric['center_xy_mm'];a=np.deg2rad(metric['yaw_deg'])
    R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
    offsets=np.array([[0,0],[17,0],[-10,0],[0,17],[0,-10]])@R.T
    world=np.c_[offsets+np.array([cx,cy]),np.full(5,z_mm)]
    T=np.linalg.inv(np.array(profile['extrinsics']['base_from_camera']));camera=world@T[:3,:3].T+T[:3,3]
    uv,_=cv2.projectPoints(camera,np.zeros(3),np.zeros(3),np.array(profile['intrinsics']['K']),np.array(profile['intrinsics']['D']))
    return uv.reshape(-1,2).tolist()
