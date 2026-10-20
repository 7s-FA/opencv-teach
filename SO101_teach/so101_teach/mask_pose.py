"""Pallet CAD silhouette fitting; carrier OpenCV image-rim fitting."""
from copy import deepcopy
import cv2
import numpy as np
from .lens_geometry import DetectionFrame,detection_region
from .height_reference import detection_plane_z
from .vision import world_xy,fit_planar_rectangle,detect


def mask_candidate(frame,mesh,roi,profile,mask,prepared=None,*,diagnostics=None):
    def rejected(reason,error,**details):
        if diagnostics is not None:diagnostics.update(rejection_reason=reason,error=error,**details)
        return None
    prepared=prepared or DetectionFrame(frame,profile)
    if prepared.error:return rejected('calibration_mismatch',prepared.error)
    if mesh.get('shape')=='rectangle':prepared=detection_region(prepared,roi)
    geometry=deepcopy(prepared.profile);geometry['table_z_mm']=profile['table_z_mm']
    raw=prepared.search_mask(mask.astype(np.uint8)*255);allowed=prepared.mask(roi)
    coverage=np.count_nonzero(raw & allowed)/max(np.count_nonzero(raw),1)
    if coverage<.98:return rejected('roi',f'지그 마스크가 검출 ROI 경계를 벗어남 · 범위 안 {coverage:.1%} (필요 98%)',
                                   roi_visible_fraction=float(coverage),roi_required_fraction=.98)
    contours,_=cv2.findContours(raw,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    if not contours:return rejected('mask_geometry','지그 라벨은 검출됐으나 마스크 외곽을 확인하지 못했습니다.')
    contour=max(contours,key=cv2.contourArea)
    if cv2.contourArea(contour)<900:return rejected('mask_too_small','지그 라벨은 검출됐으나 마스크 면적이 너무 작습니다.')
    z=detection_plane_z(geometry,mesh)
    try:xy=world_xy(contour[:,0,:],geometry,z).astype(np.float32)
    except ValueError:return rejected('projection','지그 마스크의 평면 좌표를 계산하지 못했습니다.')
    quad=cv2.boxPoints(cv2.minAreaRect(xy))
    fit=fit_planar_rectangle(quad,mesh['size_mm'][:2],geometry,z)
    if fit is not None:quad=fit['xy']
    else:
        # A whole-object mask includes side walls. Its size is not a valid rim
        # measurement, but its approximate centre/axis can still seed the fit.
        sides=np.linalg.norm(np.roll(quad,-1,axis=0)-quad,axis=1);width,height=mesh['size_mm'][:2]
        if abs(sides[1]-width)+abs(sides[0]-height)<abs(sides[0]-width)+abs(sides[1]-height):quad=np.roll(quad,-1,axis=0)
    center=quad.mean(0);edge=quad[1]-quad[0]
    period=90 if max(mesh['size_mm'][:2])/min(mesh['size_mm'][:2])<1.01 else 180
    return {'center_xy_mm':center.tolist(),'yaw_deg':float(np.degrees(np.arctan2(edge[1],edge[0]))%period),
            'symmetry_deg':period,'dimension_verified':fit is not None,'verified_for_motion':False}


def mask_search_region(mask,box):
    contours,_=cv2.findContours(mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    filled=np.zeros(mask.shape,np.uint8);cv2.drawContours(filled,contours,-1,255,cv2.FILLED)
    margin=max(16,min(64,int(np.ceil(.12*max(box[2]-box[0],box[3]-box[1])))))
    allowed=cv2.dilate(filled,np.ones((2*margin+1,2*margin+1),np.uint8))
    x,y,w,h=cv2.boundingRect(allowed)
    return allowed,{'box':[x,y,x+w,y+h],'source':'model_mask'}


def detect_mask_seeded(frame,mesh,roi,profile,detections,settings,*,prepared=None):
    """Retain ambiguity and reject failed fits; a mask itself is never adopted."""
    prepared=prepared or DetectionFrame(frame,profile)
    if prepared.error:
        return {'selected':None,'candidates':[],'status':'calibration_mismatch','error':prepared.error,'model_regions':[],'verified_for_motion':False}
    shape=mesh.get('shape','ring')
    if shape not in ('ring','rectangle'):raise ValueError('이 지그 형상에는 마스크 모델이 연결되지 않았습니다.')
    target=0 if shape=='rectangle' else 1;matches=[];regions=[];last=None;model_detected=False
    for row in sorted(detections,key=lambda row:-row.get('confidence',0)):
        if row.get('class_id')!=target or row.get('confidence',0)<settings['confidence']:continue
        model_detected=True
        mask=row.get('mask');box=np.asarray(row.get('box'),float)
        if (not isinstance(mask,np.ndarray) or mask.dtype!=np.bool_ or mask.shape!=frame.shape[:2]
                or box.shape!=(4,) or not np.isfinite(box).all()):
            raise ValueError('현재 사진의 지그 마스크가 없습니다.')
        if shape=='ring':
            from .cad_pallet import detect as detect_cad_pallet
            _,region=mask_search_region(mask,box)
            raw=detect_cad_pallet(frame,mesh,roi,profile,mask,prepared=prepared)
        else:
            diagnostics={}
            candidate=mask_candidate(frame,mesh,roi,profile,mask,prepared,diagnostics=diagnostics)
            if candidate is None:
                last={'selected':None,'candidates':[],'status':'mask_rim_not_found',
                      'model_detected':True,**diagnostics}
                continue
            search,region=mask_search_region(mask,box)
            seed=[*candidate['center_xy_mm'],np.deg2rad(candidate['yaw_deg'])]
            raw=detect(frame,mesh,roi,profile,prepared=prepared,search_mask=search,pose_seed=seed)
            raw['mask_seed']=candidate
        regions.append(region)
        raw.update(model_regions=[region]);last=raw;chosen=raw.get('selected')
        if chosen is None:
            raw['model_detected']=True
            continue
        duplicate=next((i for i,old in enumerate(matches)
                        if np.linalg.norm(np.asarray(old['selected']['center_px'])-chosen['center_px'])<8
                        and abs(old['selected']['area_px']-chosen['area_px'])/max(chosen['area_px'],1)<.12),None)
        if duplicate is None:matches.append(raw)
        elif chosen['score']>matches[duplicate]['selected']['score']:matches[duplicate]=raw
    if len(matches)==1:return matches[0]
    if len(matches)>1:
        return {'selected':None,'candidates':[row['selected'] for row in matches],'status':'ambiguous',
                'error':'같은 범위에서 여러 지그 테두리가 확인됐습니다. 범위를 구분하세요.','model_regions':regions,'model_detected':True,'verified_for_motion':False}
    return {**(last or {}),'selected':None,'candidates':[],'model_regions':regions,
            'status':last['status'] if last else 'model_not_found',
            'error':(last or {}).get('error') or ('AI 마스크 후보에서 지그 상단 테두리를 확인하지 못했습니다.' if model_detected else 'AI 모델에서 해당 지그 라벨을 찾지 못했습니다.'),
            'model_detected':model_detected,'verified_for_motion':False}
