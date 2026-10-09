"""Track a user-confirmed empty pallet's visible rim, excluding product pixels."""
import hashlib,json
from pathlib import Path
import cv2
import numpy as np
from .height_reference import support_bottom_z
from .vision import world_xy,project_plane,fit_planar_rectangle


def camera_signature(profile):
    intr=profile.get('intrinsics',{})
    value={k:intr.get(k) for k in ('K','D','size')};value['source']=profile.get('camera',{}).get('source')
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


class PalletReference:
    def __init__(self,directory):
        directory=Path(directory);self.data=json.loads((directory/'pallet.json').read_text())
        self.frame=cv2.imread(str(directory/self.data['image']))
        if self.frame is None:raise ValueError('빈 팔레트 기준 영상 없음')
        self.gray=cv2.cvtColor(self.frame,cv2.COLOR_BGR2GRAY);quad=np.asarray(self.data['quad'],np.float32);center=quad.mean(0)
        mask=np.zeros(self.gray.shape,np.uint8)
        outer=np.asarray(self.data.get('tracking_outline',center+(quad-center)*1.20),np.float32)
        cv2.fillConvexPoly(mask,cv2.convexHull(np.rint(outer).astype(np.int32)),255)
        mask=cv2.dilate(mask,np.ones((5,5),np.uint8))
        cv2.fillConvexPoly(mask,np.rint(center+(quad-center)*.70).astype(np.int32),0)
        self.points=cv2.goodFeaturesToTrack(self.gray,80,.01,4,mask=mask,blockSize=5)
    def locate(self,frame,profile,config):
        if camera_signature(profile)!=self.data['camera_signature'] or frame.shape!=self.frame.shape or self.points is None:return None
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        points,ok,error=cv2.calcOpticalFlowPyrLK(self.gray,gray,self.points,None,winSize=(11,11),maxLevel=0)
        reverse,back,_=cv2.calcOpticalFlowPyrLK(gray,self.gray,points,None,winSize=(11,11),maxLevel=0)
        old=self.points[:,0];new=points[:,0];valid=(ok[:,0]>0)&(back[:,0]>0)&(np.linalg.norm(reverse[:,0]-old,axis=1)<.8)&(error[:,0]<25)
        hsv=cv2.cvtColor(frame,cv2.COLOR_BGR2HSV);xy=np.rint(new).astype(int)
        inside=(xy[:,0]>=0)&(xy[:,0]<frame.shape[1])&(xy[:,1]>=0)&(xy[:,1]<frame.shape[0]);valid&=inside
        at=xy.clip([0,0],[frame.shape[1]-1,frame.shape[0]-1]);valid &= hsv[at[:,1],at[:,0],1]<65
        if valid.sum()<6:return None
        H,inliers=cv2.estimateAffinePartial2D(old[valid],new[valid],method=cv2.RANSAC,ransacReprojThreshold=1.2,maxIters=1000)
        if H is None or inliers.sum()<6 or inliers.mean()<.65:return None
        support=old[valid][inliers[:,0]>0];quad=np.asarray(self.data['quad'],np.float32)
        if cv2.contourArea(cv2.convexHull(support))<cv2.contourArea(quad)*.15:return None
        scale=np.linalg.norm(H[:,0])
        if not .97<scale<1.03:return None
        tracked=cv2.transform(quad[None],H)[0];z=support_bottom_z(config,profile)+20
        xy=world_xy(tracked,profile,z);fit=fit_planar_rectangle(xy,[70,70],profile,z)
        if fit is None:return None
        q=project_plane(fit['xy'],profile,z);edge=fit['xy'][1]-fit['xy'][0]
        measured=np.linalg.norm(tracked.mean(0)-quad.mean(0))
        selected={'quad':q.tolist(),'center_px':q.mean(0).tolist(),'metric':{'center_xy_mm':fit['center'].tolist(),'yaw_deg':float(np.degrees(np.arctan2(edge[1],edge[0]))%90),'symmetry_deg':90},'tracking_points':int(inliers.sum()),'reference_shift_px':float(measured)}
        return {'selected':selected,'inspection_only':True,'inspection_source':'빈 팔레트 기준 · 현재 외곽 추적','status':'inspection_rim_tracking'}
