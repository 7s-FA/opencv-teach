"""Constrain a failed pallet mask fit with a supported projected CAD body ROI.

The original mask/ROI gate remains authoritative. Only a small thin protrusion
may be excluded, and the resulting pose still needs original-contour support.
Existing accepted poses are returned unchanged by the caller.
"""
import cv2
import numpy as np
from . import cad_pallet as cad

def project_hull(pose,mesh,profile):
 points=cad.model_hull(mesh['sha256']).copy();points[:,:2]-=np.asarray(mesh['low_mm'][:2])+np.asarray(mesh['size_mm'][:2])/2;points[:,2]+=profile['table_z_mm']-mesh['low_mm'][2]
 a=np.deg2rad(pose['yaw_deg']);c,s=np.cos(a),np.sin(a);points[:,:2]=points[:,:2]@np.array([[c,s],[-s,c]])+pose['center_xy_mm'];T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));camera=points@T[:3,:3].T+T[:3,3];uv=camera@np.asarray(profile['intrinsics']['K']).T;uv=uv[:,:2]/uv[:,2:];return cv2.convexHull(uv.astype(np.float32))[:,0,:]

def difference(a,b):return [float(np.linalg.norm(np.asarray(a['center_xy_mm'])-b['center_xy_mm'])),float(abs((a['yaw_deg']-b['yaw_deg']+45)%90-45))]

def recover_fit(mask,mesh,profile,original,diagnostics=None):
 d=diagnostics if diagnostics is not None else {}
 if original is None or original['rms_px']<=3 or not original['converged'] or original['at_bound'] or not original['pose_constrained']:return original
 area=np.count_nonzero(mask);count,labels,stats,_=cv2.connectedComponentsWithStats((mask>0).astype(np.uint8),8)
 if count<2 or stats[1:,cv2.CC_STAT_AREA].max()/area<.95:d['reason']='components';return original
 radius=max(1,int(round(np.sqrt(area)*.045)));radius=min(radius,6);distance=cv2.distanceTransform((mask>0).astype(np.uint8),cv2.DIST_L2,3)
 contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE);contour=cad.resample(max(contours,key=cv2.contourArea)[:,0,:].astype(float),512);variants=[]
 d.update(area=area,radius=radius)
 for k in [2*radius+1,2*radius+3]:
  core=cv2.morphologyEx(mask,cv2.MORPH_OPEN,np.ones((k,k),np.uint8));seed=cad.fit_pose(core,mesh,profile)
  if not seed or not seed['converged'] or seed['at_bound'] or seed['rms_px']>3 or not seed['pose_constrained']:d['reason']='body_seed';d['failed_seed']=seed;d['kernel']=k;return original
  poly=project_hull(seed['pose'],mesh,profile);region=np.zeros_like(mask);cv2.fillConvexPoly(region,np.rint(poly).astype(np.int32),255);region=cv2.dilate(region,np.ones((5,5),np.uint8));retained=cv2.bitwise_and(mask,region);removed=(mask>0)&(retained==0);fraction=np.count_nonzero(removed)/area
  if not 0<fraction<=.03 or distance[removed].max()>np.sqrt(2)*radius:d.update(reason='not_minor_thin_protrusion',removed_fraction=float(fraction),max_radius=float(distance[removed].max()) if removed.any() else 0.,kernel=k);return original
  fitted=cad.fit_pose(retained,mesh,profile)
  if not fitted or not fitted['converged'] or fitted['at_bound'] or fitted['rms_px']>3 or not fitted['pose_constrained']:d['reason']='retained_fit';return original
  if max(difference(seed['pose'],fitted['pose']))>.5:d['reason']='seed_disagreement';return original
  # Validate on the original observed contour, excluding no pixels from this check.
  polygon=project_hull(fitted['pose'],mesh,profile);residual=cad.edge_distance(contour,polygon);support=cad.boundary_support(contour,polygon);inliers=residual<=3
  if inliers.mean()<.80 or support['observed_sides']<3 or not support['pose_constrained']:d['reason']='original_contour_support';return original
  variants.append({'kernel':k,'seed':seed,'fitted':fitted,'removed_fraction':fraction,'removed_max_radius':float(distance[removed].max()),'original_contour_inlier_fraction':float(inliers.mean()),'original_support':support})
 agreement=difference(variants[0]['fitted']['pose'],variants[1]['fitted']['pose']);d.update(variants=variants,agreement=agreement)
 if max(agreement)>.2:d['reason']='shape_roi_unstable';return original
 d['reason']='recovered';answer=variants[0]['fitted'];answer['cad_shape_roi']={'original_rms_px':original['rms_px'],'removed_fraction':variants[0]['removed_fraction'],'original_contour_inlier_fraction':variants[0]['original_contour_inlier_fraction'],'seed_variants_pose_difference_mm_deg':agreement,'roi_kind':'projected_3d_cad','source_mask_unchanged':True,'roi_margin_rectified_px':2};return answer
