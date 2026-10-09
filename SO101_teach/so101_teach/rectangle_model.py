"""Fit a known rectangular rim to independently observed sides at image borders."""
import cv2
import numpy as np
from .height_reference import detection_plane_z


def quad_visibility(quad,allowed,visible):
    """Allow only camera clipping; user-selected ROI boundaries remain strict."""
    points=np.rint(quad).astype(np.int32);x0,y0=points.min(0);x1,y1=points.max(0)+1
    if x1-x0>allowed.shape[1]*2 or y1-y0>allowed.shape[0]*2:return 0.
    footprint=np.zeros((y1-y0,x1-x0),np.uint8);cv2.fillConvexPoly(footprint,points-[x0,y0],255)
    left,top=max(x0,0),max(y0,0);right,bottom=min(x1,allowed.shape[1]),min(y1,allowed.shape[0])
    if left>=right or top>=bottom:return 0.
    part=footprint[top-y0:bottom-y0,left-x0:right-x0]>0
    observed=part&(visible[top:bottom,left:right]>0)
    if np.any(observed&(allowed[top:bottom,left:right]==0)):return 0.
    return float(np.count_nonzero(observed)/max(np.count_nonzero(footprint),1))


def rectangle_proposals(frame,mesh,allowed,profile,visible,*,seed_poses=None,evidence=None,_rim_scale=None):
    from .vision import ordered_quad,world_xy,project_plane
    if mesh.get('shape')!='rectangle' or not profile or not profile.get('intrinsics') or not profile.get('extrinsics'):return []
    if seed_poses is not None and mesh.get('assembly') and _rim_scale is None:
        # Compare the entire existing 3% size allowance. Stopping at the first
        # passing scale can select an inner edge that fails the grid check,
        # hiding a better-supported outer rim at another admissible scale.
        proposals=[]
        for scale in (1.,.99,1.01,.98,1.02,.97,1.03):
            proposals.extend(rectangle_proposals(frame,mesh,allowed,profile,visible,seed_poses=seed_poses,evidence=evidence,_rim_scale=scale))
        best=[]
        for item in sorted(proposals,key=lambda p:p['edge_cost']):
            if not any(np.linalg.norm(item['quad'].mean(0)-p['quad'].mean(0))<8 for p in best):best.append(item)
        return best
    width,height=np.asarray(mesh['size_mm'][:2])*(1. if _rim_scale is None else _rim_scale);z=detection_plane_z(profile,mesh)
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    gray=evidence.source.blurred()
    edges=evidence.edges()
    seeds=[]
    if seed_poses is not None:
        for seed in seed_poses:
            cx,cy,angle=seed;c,s=np.cos(angle),np.sin(angle);rotation=np.array([[c,-s],[s,c]])
            outer=np.array([[-width,-height],[width,-height],[width,height],[-width,height]])/2
            q=project_plane(outer@rotation.T+[cx,cy],profile,z)
            if quad_visibility(q,allowed,visible)>=.90:seeds.append(np.asarray(seed,float))
    else:
        contours,_=cv2.findContours(edges,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
        for contour in sorted(contours,key=lambda c:cv2.arcLength(c,False),reverse=True):
            if cv2.arcLength(contour,False)<100:continue
            xy=world_xy(contour[:,0,:],profile,z)
            (cx,cy),(w,h),angle=cv2.minAreaRect(np.float32(xy))
            if min(w,h)<=0:continue
            if abs(w-width)+abs(h-height)>abs(h-width)+abs(w-height):w,h=h,w;angle+=90
            if max(abs(w/width-1),abs(h/height-1))>.32:continue
            if any(np.linalg.norm(np.array([cx,cy])-s[:2])<12 for s in seeds):continue
            seeds.append(np.array([cx,cy,np.deg2rad(angle)]))
            if len(seeds)>=4:break
    if not seeds:return []
    distance=evidence.distance()
    outer=np.array([[-width,-height],[width,-height],[width,height],[-width,height]])/2
    along=np.linspace(.08,.92,32)
    sample=np.concatenate([a+(b-a)*along[:,None] for a,b in zip(outer,np.roll(outer,-1,axis=0))])
    T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));K=np.asarray(profile['intrinsics']['K'])
    # All samples lie on one plane. Project with the equivalent homography
    # without allocating and multiplying an N-by-128-by-3 world/camera array.
    H=K@np.column_stack((T[:3,0],T[:3,1],T[:3,2]*z+T[:3,3]))
    def evaluate(poses,strict=True):
        c=np.cos(poses[:,2,None]);s=np.sin(poses[:,2,None])
        x=c*sample[:,0]-s*sample[:,1]+poses[:,0,None]
        y=s*sample[:,0]+c*sample[:,1]+poses[:,1,None]
        depth=H[2,0]*x+H[2,1]*y+H[2,2]
        uv=np.stack(((H[0,0]*x+H[0,1]*y+H[0,2])/depth,
                     (H[1,0]*x+H[1,1]*y+H[1,2])/depth),axis=-1)
        pixels=np.rint(uv).astype(int);h,w=gray.shape
        inside=(pixels[:,:,0]>=0)&(pixels[:,:,0]<w)&(pixels[:,:,1]>=0)&(pixels[:,:,1]<h)
        px=np.clip(pixels[:,:,0],0,w-1);py=np.clip(pixels[:,:,1],0,h-1)
        observed=inside&(visible[py,px]>0);in_roi=allowed[py,px]>0
        count=observed.reshape(-1,4,32).sum(2);d=distance[py,px]
        support=((d<2.5)&observed).reshape(-1,4,32).sum(2)/np.maximum(count,1)
        cost=(np.minimum(d,5)/5*observed).reshape(-1,4,32).sum(2)/np.maximum(count,1)
        valid=(count.min(1)>=24)&(support.min(1)>=.65)&(support.mean(1)>=.8)&~(observed&~in_roi).any(1)
        score=cost.mean(1)
        if strict:valid &=score<=.20
        else:valid=(count.min(1)>=24)&~(observed&~in_roi).any(1)
        score[~valid]=np.inf
        return score,support
    span,step,turn=(24,4,8) if seed_poses is None else (8,2,4)
    grid=np.stack(np.meshgrid(np.arange(-span,span+.1,step),np.arange(-span,span+.1,step),np.deg2rad(np.arange(-turn,turn+.1,2)),indexing='ij'),axis=-1).reshape(-1,3)
    fine=np.stack(np.meshgrid(np.arange(-3,3.1,1),np.arange(-3,3.1,1),np.deg2rad(np.arange(-2,2.1,.5)),indexing='ij'),axis=-1).reshape(-1,3)
    proposals=[]
    for seed in seeds:
        poses=grid+seed;scores,support=evaluate(poses,strict=False)
        if not np.isfinite(scores).any():continue
        poses=fine+poses[np.argmin(scores)];scores,support=evaluate(poses);i=int(np.argmin(scores))
        if not np.isfinite(scores[i]):continue
        cx,cy,a=poses[i];R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
        q=ordered_quad(project_plane(outer@R.T+[cx,cy],profile,z));fraction=quad_visibility(q,allowed,visible)
        if fraction<.90:continue
        if any(np.linalg.norm(q.mean(0)-p['quad'].mean(0))<8 for p in proposals):continue
        proposals.append({'quad':q,'edge_support':support[i].tolist(),'edge_cost':float(scores[i]),'visible_fraction':fraction})
    return proposals
