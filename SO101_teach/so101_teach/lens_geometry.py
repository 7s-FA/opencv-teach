"""One rectified detection image; public observations stay in raw image pixels."""
from copy import deepcopy
from functools import lru_cache
import cv2
import numpy as np

@lru_cache(maxsize=4)
def lens_maps(size,k,d):
    K=np.asarray(k,float).reshape(3,3);D=np.asarray(d,float)
    # alpha=1 preserves the sensor field of view, including edge-mounted jigs.
    newK,_=cv2.getOptimalNewCameraMatrix(K,D,size,1,size)
    maps=cv2.initUndistortRectifyMap(K,D,None,newK,size,cv2.CV_32FC1)
    # Reject folded/extrapolated borders of the distortion polynomial.
    dxdu,dxdv=cv2.Sobel(maps[0],cv2.CV_32F,1,0,scale=1/8),cv2.Sobel(maps[0],cv2.CV_32F,0,1,scale=1/8)
    dydu,dydv=cv2.Sobel(maps[1],cv2.CV_32F,1,0,scale=1/8),cv2.Sobel(maps[1],cv2.CV_32F,0,1,scale=1/8)
    valid=((dxdu>0)&(dydv>0)&(dxdu*dydv-dxdv*dydu>.01)).astype(np.uint8)*255
    return newK,maps,valid

class DetectionFrame:
    def __init__(self,frame,profile):
        self.frame=frame;self.profile=deepcopy(profile);self.maps=None;self.error=None;self.mask_cache={};self.evidence=None
        self.height,self.width=frame.shape[:2]
        intrinsics=(profile or {}).get('intrinsics')
        if not intrinsics:return
        if list(intrinsics['size'])!=[self.width,self.height]:
            self.error='현재 영상 해상도와 카메라 보정 해상도가 다릅니다. 설정에서 보정을 확인하세요.';return
        camera=(profile or {}).get('camera',{});cal_camera=intrinsics.get('camera',{})
        if camera.get('source') is not None and cal_camera.get('source') is not None and str(camera['source'])!=str(cal_camera['source']):
            self.error='현재 카메라와 렌즈 보정의 카메라가 다릅니다. 해당 카메라의 보정을 불러오세요.';return
        self.K=np.asarray(intrinsics['K'],float);self.D=np.asarray(intrinsics['D'],float)
        if not np.any(self.D):return
        self.newK,self.maps,self.valid=lens_maps((self.width,self.height),tuple(self.K.ravel()),tuple(self.D.ravel()))
        self.frame=cv2.remap(frame,*self.maps,cv2.INTER_LINEAR)
        self.profile['intrinsics']={**intrinsics,'K':self.newK.tolist(),'D':np.zeros_like(self.D).tolist()}
    def mask(self,roi):
        from .roi_geometry import roi_mask
        key=None if roi is None else tuple(np.asarray(roi,float).ravel())
        if key not in self.mask_cache:
            mask=roi_mask(roi,self.width,self.height)
            if self.maps:mask=cv2.bitwise_and(cv2.remap(mask,*self.maps,cv2.INTER_NEAREST),self.valid)
            self.mask_cache[key]=mask
        return self.mask_cache[key]
    def raw_points(self,points):
        points=np.asarray(points,float).reshape(-1,2)
        if self.maps is None:return points
        rays=cv2.undistortPoints(points.reshape(-1,1,2),self.newK,None).reshape(-1,2)
        uv,_=cv2.projectPoints(np.c_[rays,np.ones(len(rays))],np.zeros(3),np.zeros(3),self.K,self.D)
        return uv.reshape(-1,2)
    def restore(self,result):
        result['lens_corrected']=self.maps is not None
        if self.maps is None and not getattr(self,'offset',None):return result
        seen=set()
        for item in [*result['candidates'],result.get('selected')]:
            if item is None or id(item) in seen:continue
            seen.add(id(item));q=np.asarray(item['quad'])
            border=np.concatenate([a+(b-a)*np.linspace(0,1,24,endpoint=False)[:,None] for a,b in zip(q,np.roll(q,-1,axis=0))])
            item['outline_px']=self.raw_points(border).tolist()
            item['quad']=self.raw_points(q).tolist();item['center_px']=self.raw_points([item['center_px']])[0].tolist()
            if item.get('axes_px') is not None:item['axes_px']=self.raw_points(item['axes_px']).tolist()
            if item.get('grid_crossings_px') is not None:item['grid_crossings_px']=self.raw_points(item['grid_crossings_px']).tolist()
            for key in ('orientation_hole_px','orientation_hole_outline_px'):
                if item.get(key) is not None:
                    points=self.raw_points(item[key]);item[key]=(points[0] if key=='orientation_hole_px' else points).tolist()
            q=np.asarray(item['quad']);edge=q[1]-q[0]
            item['image_angle_deg']=float(np.degrees(np.arctan2(edge[1],edge[0]))%item['symmetry_deg'])
            item['area_px']=float(cv2.contourArea(np.float32(item['outline_px'])))
        return result


class DetectionRegion(DetectionFrame):
    """Padded ROI view of a rectified frame, retaining original public pixels."""
    def __init__(self,parent,bounds):
        x,y,w,h=bounds;self.parent=parent;self.offset=(x,y)
        self.frame=parent.frame[y:y+h,x:x+w];self.width=w;self.height=h
        self.maps=parent.maps;self.error=parent.error;self.evidence=None
        self.profile=deepcopy(parent.profile)
        self.profile['_detection_image_area']=parent.width*parent.height
        intrinsics=self.profile['intrinsics'];k=np.array(intrinsics['K'],float)
        k[0,2]-=x;k[1,2]-=y
        intrinsics.update(K=k.tolist(),size=[w,h])
    def mask(self,roi):
        x,y=self.offset
        return self.parent.mask(roi)[y:y+self.height,x:x+self.width]
    def raw_points(self,points):
        return self.parent.raw_points(np.asarray(points).reshape(-1,2)+self.offset)


def detection_region(prepared,roi):
    if roi is None or prepared.error or not (prepared.profile or {}).get('intrinsics'):return prepared
    x,y,w,h=cv2.boundingRect(prepared.mask(roi))
    if not w or not h:return prepared
    # Visible padding preserves real ROI rejection: the crop border must not
    # masquerade as the sensor border for partially visible model proposals.
    left=max(0,x-32);top=max(0,y-32)
    right=min(prepared.width,x+w+32);bottom=min(prepared.height,y+h+32)
    if (right-left)*(bottom-top)>=prepared.width*prepared.height*.8:return prepared
    return DetectionRegion(prepared,(left,top,right-left,bottom-top))
