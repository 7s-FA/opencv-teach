"""Fit the fixed CAD silhouette to the current AI mask.

Pallet acceptance uses CAD fit, calibration and ROI only; RGB edges, colors,
openings and raised features do not refine or validate its position.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib,re
import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial import ConvexHull
from .domain import ROOT
from .height_reference import detection_plane_z
from .lens_geometry import DetectionFrame

MAX_CAD_ERROR_PX=3.0

@lru_cache(maxsize=8)
def model_hull(sha):
    if not isinstance(sha,str) or len(sha)!=64 or any(c not in '0123456789abcdef' for c in sha):raise ValueError('팔레트 CAD 식별자 오류')
    raw=(ROOT/'data/jig_assets'/(sha+'.stl')).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('팔레트 CAD 파일 확인 실패')
    count=int.from_bytes(raw[80:84],'little') if len(raw)>=84 else 0
    if count and len(raw)==84+50*count:
        points=np.frombuffer(raw[84:],dtype=np.dtype([('normal','<f4',3),('v','<f4',(3,3)),('attr','<u2')]))['v'].reshape(-1,3).astype(float)
    else:points=np.asarray(re.findall(r'\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)',raw.decode('ascii')),float)
    if points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all():raise ValueError('팔레트 CAD 좌표 오류')
    points=np.unique(points,axis=0);return points[ConvexHull(points).vertices]

def resample(polygon,n=128):
    delta=np.roll(polygon,-1,axis=0)-polygon;length=np.linalg.norm(delta,axis=1);total=length.sum()
    cumulative=np.r_[0,np.cumsum(length)];s=np.arange(n)*total/n;i=np.minimum(np.searchsorted(cumulative,s,side='right')-1,len(polygon)-1)
    return polygon[i]+delta[i]*((s-cumulative[i])/np.maximum(length[i],1e-9))[:,None]

def edge_distance(points,polygon):
    a=polygon;delta=np.roll(a,-1,axis=0)-a
    t=np.clip(np.sum((points[:,None,:]-a)*delta,axis=2)/np.maximum(np.sum(delta*delta,axis=1),1e-12),0,1)
    return np.sqrt(np.maximum(np.min(np.sum((points[:,None,:]-a-t[:,:,None]*delta)**2,axis=2),1),1e-16))

def observed_boundary(mask,points):
    # Convex hull chords across occlusion are not observations. Check binary
    # segmentation support only; no RGB edges, colors or previous poses.
    xy=np.rint(points).astype(int);supported=np.zeros(len(xy),bool)
    for dx in (-1,0,1):
        for dy in (-1,0,1):
            x,y=xy[:,0]+dx,xy[:,1]+dy;inside=(x>=0)&(x<mask.shape[1])&(y>=0)&(y<mask.shape[0])
            supported[inside]|=mask[y[inside],x[inside]]>0
    return supported

def boundary_support(samples,polygon):
    """Long nonparallel CAD edges constrain both translation axes and yaw."""
    a=polygon;d=np.roll(a,-1,axis=0)-a;length=np.linalg.norm(d,axis=1)
    t=np.clip(np.sum((samples[:,None,:]-a)*d,axis=2)/np.maximum(length**2,1e-12),0,1)
    distances=np.linalg.norm(samples[:,None,:]-a-t[:,:,None]*d,axis=2);nearest=distances.argmin(1)
    spans=[]
    for i in range(len(a)):
        values=t[(nearest==i)&(distances[:,i]<=MAX_CAD_ERROR_PX),i]
        spans.append(float(np.ptp(values)) if len(values)>=2 else 0.)
    good=[i for i in range(len(a)) if length[i]>=.35*length.max() and spans[i]>=.5]
    # Parallel opposite edges cannot constrain translation along those edges.
    # Two extended, nonparallel edges do constrain a known-size CAD pose; a
    # third side is not required when the product hides it.
    cross=max((abs(d[i,0]*d[j,1]-d[i,1]*d[j,0])/(length[i]*length[j])
               for n,i in enumerate(good) for j in good[n+1:]),default=0.)
    return {'observed_sides':len(good),'side_cross':float(cross),'pose_constrained':bool(cross>=.5)}

def fit_pose(mask,mesh,profile):
    from .vision import world_xy,fit_planar_rectangle
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    if not contours:return None
    contour=max(contours,key=cv2.contourArea)
    if cv2.contourArea(contour)<900:return None
    z=detection_plane_z(profile,mesh);xy=world_xy(contour[:,0,:],profile,z).astype(np.float32)
    quad=cv2.boxPoints(cv2.minAreaRect(xy));initial_fit=fit_planar_rectangle(quad,mesh['size_mm'][:2],profile,z)
    if initial_fit is not None:quad=initial_fit['xy']
    center=quad.mean(0);edge=quad[1]-quad[0];angle=np.arctan2(edge[1],edge[0])% (np.pi/2)
    polygon=cv2.convexHull(contour)[:,0,:].astype(float);samples=resample(polygon)
    observed=observed_boundary(mask,samples);missing=~observed
    # Three consecutive unsupported samples identify an occlusion chord;
    # one or two isolated raster gaps retain the original full-silhouette fit.
    partial=bool(np.any(missing & np.roll(missing,1) & np.roll(missing,2)));visible_samples=samples[observed]
    if partial and observed.mean()<.60:return None
    points=model_hull(mesh['sha256']).copy()
    if not np.allclose(points.min(0),mesh['low_mm'],atol=.01) or not np.allclose(np.ptp(points,axis=0),mesh['size_mm'],atol=.01):raise ValueError('팔레트 CAD 치수와 설정이 다릅니다.')
    points[:,:2]-=np.asarray(mesh['low_mm'][:2])+np.asarray(mesh['size_mm'][:2])/2
    points[:,2]+=profile['table_z_mm']-mesh['low_mm'][2]
    T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));K=np.asarray(profile['intrinsics']['K'])
    def projected(parameters):
        x,y,a=parameters;c,s=np.cos(a),np.sin(a);world=points.copy();world[:,:2]=points[:,:2]@np.array([[c,s],[-s,c]])+[x,y]
        camera=world@T[:3,:3].T+T[:3,3]
        if np.any(camera[:,2]<=0):raise ValueError('팔레트 CAD가 카메라 뒤에 있습니다.')
        uv=camera@K.T;uv=uv[:,:2]/uv[:,2:];indices=cv2.convexHull(uv.astype(np.float32),returnPoints=False).ravel();return uv[indices]
    def residual(parameters):
        predicted=projected(parameters)
        if partial:return edge_distance(visible_samples,predicted)
        return np.r_[edge_distance(samples,predicted),edge_distance(resample(predicted),polygon)]
    initial=np.r_[center,angle];fit=least_squares(residual,initial,bounds=(initial-[20,20,.15],initial+[20,20,.15]),x_scale=[5,5,.05],loss='soft_l1',f_scale=1.,max_nfev=60,ftol=1e-7,xtol=1e-7,gtol=1e-7)
    rms=float(np.sqrt(np.mean(fit.fun**2)))
    return {'pose':{'center_xy_mm':fit.x[:2].tolist(),'yaw_deg':float(np.rad2deg(fit.x[2])%90),'symmetry_deg':90},'seed':{'center_xy_mm':center.tolist(),'yaw_deg':float(np.rad2deg(angle)),'symmetry_deg':90},'rms_px':rms,'rms_limit_px':MAX_CAD_ERROR_PX,'observed_fraction':float(observed.mean()),**(boundary_support(visible_samples,projected(fit.x)) if partial else {'observed_sides':4,'side_cross':1.,'pose_constrained':True}),'partial_silhouette':partial,'converged':bool(fit.success),'at_bound':bool(np.any(abs(fit.x-initial)>np.array([19.9,19.9,.149])))}

def detect(frame,mesh,roi,profile,mask,*,prepared=None,recover_shape=False):
    from .vision import quad_inside_mask,rectangle_quality,projected_jig_axes,project_plane
    def rejected(reason,**extra):return {'selected':None,'candidates':[],'status':'mask_rim_not_found','cad_pallet_reason':reason,'error':'팔레트 CAD 정합 미통과: '+reason,'verified_for_motion':False,**extra}
    if (mesh.get('shape')!='ring' or mesh.get('unit','mm')!='mm' or not np.allclose(mesh.get('size_mm',[0,0,0])[:2],[70,70],atol=.1)):return rejected('unsupported_cad')
    prepared=prepared or DetectionFrame(frame,profile)
    if prepared.error:return {'selected':None,'candidates':[],'status':'calibration_mismatch','error':prepared.error,'verified_for_motion':False}
    if not profile or not profile.get('intrinsics') or not profile.get('extrinsics'):return rejected('calibration_required')
    geometry=deepcopy(prepared.profile);geometry['table_z_mm']=profile['table_z_mm'];observed=prepared.search_mask(mask.astype(np.uint8)*255);allowed=prepared.mask(roi)
    coverage=np.count_nonzero(observed&allowed)/max(np.count_nonzero(observed),1)
    if coverage<.98:
        return rejected('roi',roi_visible_fraction=float(coverage),roi_required_fraction=.98,
                        error=f'팔레트가 지그 검출 ROI 범위를 벗어남 · 범위 안 {coverage*100:.1f}% (필요 98%)')
    fitted=fit_pose(observed,mesh,geometry)
    if fitted is None:return rejected('mask_geometry')
    if recover_shape and fitted['rms_px']>MAX_CAD_ERROR_PX:
        from .pallet_shape_roi import recover_fit
        fitted=recover_fit(observed,mesh,geometry,fitted)
    metadata={'cad_fit':{k:v for k,v in fitted.items() if k not in ('pose','seed')},'mask_seed':fitted['seed'],'pallet_pose_method':'cad_silhouette_fit'}
    if not fitted['pose_constrained']:return rejected('insufficient_visible_sides',**metadata)
    if not fitted['converged'] or fitted['at_bound'] or not np.isfinite(fitted['rms_px']) or fitted['rms_px']>MAX_CAD_ERROR_PX:return rejected('cad_fit',**metadata)
    w,h=mesh['size_mm'][:2];pose=fitted['pose'];a=np.deg2rad(pose['yaw_deg']);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
    xy=np.array([[-w,-h],[w,-h],[w,h],[-w,h]])/2@R.T+pose['center_xy_mm'];metric={**pose,'sides_mm':[w,h,w,h],'dimension_source':'cad_silhouette_fit','verified':False}
    quad=project_plane(xy,geometry,detection_plane_z(geometry,mesh))
    if not quad_inside_mask(quad,allowed):
        return rejected('roi',**metadata,roi_visible_fraction=float(coverage),roi_required_fraction=.98,
                        error='CAD로 정합한 팔레트 상단 외곽이 지그 검출 ROI 범위를 벗어남')
    quality=rectangle_quality(xy,[w,h]);quality['fit_from_model']=True;center=project_plane([pose['center_xy_mm']],geometry,detection_plane_z(geometry,mesh))[0]
    chosen={'quad':quad.tolist(),'center_px':center.tolist(),'symmetry_deg':90,'image_angle_deg':float(np.rad2deg(np.arctan2(*(quad[1]-quad[0])[::-1]))%90),
            'metric':metric,'outer_shape':quality,'shape_match':True,'score':float(1-fitted['rms_px']/10),'area_px':float(cv2.contourArea(quad.astype(np.float32))),
            'cad_pose_validated':True,'cad_silhouette_rms_px':fitted['rms_px'],'partial_visible':False,'visible_fraction':1.}
    chosen['axes_px']=projected_jig_axes(chosen,geometry,detection_plane_z(geometry,mesh))
    return prepared.restore({'selected':chosen,'candidates':[chosen],'status':'shape_match','verified_for_motion':False,'mask_seed_refined':True,**metadata})
