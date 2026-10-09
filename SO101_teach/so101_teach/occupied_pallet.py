"""Inspection-only pallet registration from exposed white outer edges.
Never returns a robot/adoption pose; the workcell detector remains unchanged.
"""
import cv2
import numpy as np
from .lens_geometry import DetectionFrame
from .height_reference import support_bottom_z
from .vision import world_xy,project_plane


def locate(frame,config,profile):
    prepared=DetectionFrame(frame,profile)
    if prepared.error:return None
    im=prepared.frame;p=prepared.profile;allowed=prepared.mask(config.get('roi'))>0
    hsv=cv2.cvtColor(im,cv2.COLOR_BGR2HSV)
    colored=((hsv[:,:,1]>65)&(hsv[:,:,2]>45)&allowed).astype(np.uint8)*255
    colored=cv2.morphologyEx(colored,cv2.MORPH_CLOSE,np.ones((7,7),np.uint8))
    contours,_=cv2.findContours(colored,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    z=support_bottom_z(config,profile)+20
    edges=cv2.Canny(cv2.GaussianBlur(cv2.cvtColor(im,cv2.COLOR_BGR2GRAY),(3,3),0),18,50)
    # The colored product cannot be used as supporting pallet-edge evidence.
    occluded=cv2.dilate(colored,np.ones((7,7),np.uint8))>0
    edges[occluded | ~allowed]=0
    distance=cv2.distanceTransform(255-edges,cv2.DIST_L2,3)
    seeds=[]
    for contour in sorted(contours,key=cv2.contourArea,reverse=True)[:4]:
        if not 400<cv2.contourArea(contour)<20000:continue
        xy=world_xy(contour[:,0],p,z+30)
        center,size,angle=cv2.minAreaRect(np.float32(xy))
        if min(size)<15 or max(size)>95:continue
        seeds.append([*center,np.deg2rad(angle)])
    if not seeds:return None
    outer=np.array([[-35,-35],[35,-35],[35,35],[-35,35]],float)
    along=np.linspace(.08,.92,32);samples=np.concatenate([a+(b-a)*along[:,None] for a,b in zip(outer,np.roll(outer,-1,axis=0))])
    T=np.linalg.inv(np.asarray(p['extrinsics']['base_from_camera']));K=np.asarray(p['intrinsics']['K']);H=K@np.column_stack((T[:3,0],T[:3,1],T[:3,2]*z+T[:3,3]))
    bottom_H=K@np.column_stack((T[:3,0],T[:3,1],T[:3,2]*(z-20)+T[:3,3]))
    def evaluate(poses):
        c=np.cos(poses[:,2,None]);s=np.sin(poses[:,2,None]);x=c*samples[:,0]-s*samples[:,1]+poses[:,0,None];y=s*samples[:,0]+c*samples[:,1]+poses[:,1,None]
        depth=H[2,0]*x+H[2,1]*y+H[2,2]
        uv=np.stack(((H[0,0]*x+H[0,1]*y+H[0,2])/depth,(H[1,0]*x+H[1,1]*y+H[1,2])/depth),-1)
        pixels=np.rint(uv).astype(int);ix=np.clip(pixels[:,:,0],0,im.shape[1]-1);iy=np.clip(pixels[:,:,1],0,im.shape[0]-1)
        visible=allowed[iy,ix]&~occluded[iy,ix]
        support=((distance[iy,ix]<2.4)&visible).reshape(-1,4,32).sum(2)/32
        adjacent=np.maximum.reduce([np.minimum(support[:,i],support[:,(i+1)%4]) for i in range(4)])
        score=np.sort(support,axis=1)[:,-3:].mean(1)
        # The upper and lower white edges are 20 mm apart in height. Requiring
        # the lower edge too prevents registering that edge as the upper rim.
        bottom_depth=bottom_H[2,0]*x+bottom_H[2,1]*y+bottom_H[2,2]
        bx=np.rint((bottom_H[0,0]*x+bottom_H[0,1]*y+bottom_H[0,2])/bottom_depth).astype(int).clip(0,im.shape[1]-1)
        by=np.rint((bottom_H[1,0]*x+bottom_H[1,1]*y+bottom_H[1,2])/bottom_depth).astype(int).clip(0,im.shape[0]-1)
        lower=((distance[by,bx]<2.4)&allowed[by,bx]&~occluded[by,bx]).reshape(-1,4,32).sum(2)/32
        lower_score=np.sort(lower,axis=1)[:,-2:].mean(1)
        score=.65*score+.35*lower_score
        valid=(adjacent>=.65)&(np.sort(support,axis=1)[:,-2:].mean(1)>=.8)&allowed[iy,ix].all(1)
        valid &= lower_score>=.5
        return np.where(valid,score,-1),support
    grid=np.stack(np.meshgrid(np.arange(-20,20.1,2),np.arange(-20,20.1,2),np.deg2rad(np.arange(-12,12.1,2)),indexing='ij'),-1).reshape(-1,3)
    fine=np.stack(np.meshgrid(np.arange(-2,2.1,.5),np.arange(-2,2.1,.5),np.deg2rad(np.arange(-1,1.1,.5)),indexing='ij'),-1).reshape(-1,3)
    found=[]
    for seed in seeds:
        poses=grid+seed;scores,support=evaluate(poses)
        if scores.max()<.55:continue
        poses=fine+poses[scores.argmax()];scores,support=evaluate(poses);i=scores.argmax()
        if scores[i]<.55:continue
        cx,cy,a=poses[i];r=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
        q=prepared.raw_points(project_plane(outer@r.T+[cx,cy],p,z));center=prepared.raw_points(project_plane([[cx,cy]],p,z))[0]
        found.append({'quad':q.tolist(),'center_px':center.tolist(),'orientation_verified':False,'metric':{'center_xy_mm':[float(cx),float(cy)],'yaw_deg':float(np.rad2deg(a)%90),'symmetry_deg':90},'score':float(scores[i]),'edge_support':support[i].tolist()})
    if not found:return None
    found.sort(key=lambda r:r['score'],reverse=True)
    if len(found)>1 and np.linalg.norm(np.array(found[0]['center_px'])-found[1]['center_px'])>20 and found[0]['score']-found[1]['score']<.08:return None
    return {'selected':found[0],'status':'inspection_outer_edges','inspection_only':True}
