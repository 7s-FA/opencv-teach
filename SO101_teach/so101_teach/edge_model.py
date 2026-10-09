"""Recover a ring from its opening plus independently supported outer edges.

A hole is only a search seed. Every outer side must have image evidence before
returning a proposal to the ordinary size, ROI and opening checks.
"""
import cv2
import numpy as np
from .height_reference import detection_plane_z

def ring_proposals(frame,mesh,allowed,profile,*,evidence=None):
    from .vision import ordered_quad,world_xy,project_plane,quad_inside_mask
    if mesh.get('shape','ring')!='ring' or len(mesh.get('holes',[]))!=1 or not profile or not profile.get('extrinsics') or not profile.get('intrinsics'):return []
    width,height=mesh['size_mm'][:2];hole=mesh['holes'][0]
    hole_size=np.asarray(hole['size'])*[width,height]
    from .frame_evidence import RegionEvidence
    evidence=evidence or RegionEvidence(frame,allowed)
    gray=evidence.source.blurred(True);edges=evidence.edges(enhanced=True)
    # Bridge tiny gaps only when finding opening seeds. Every proposal is
    # still checked against the original, unclosed edge image below.
    closed=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8));closed &=allowed
    contours=list(cv2.findContours(closed,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0])
    z=detection_plane_z(profile,mesh);seeds=[]
    for contour in contours:
        if not 80<cv2.contourArea(contour)<frame.shape[0]*frame.shape[1]*.1:continue
        poly=cv2.approxPolyDP(contour,.03*cv2.arcLength(contour,True),True)
        if len(poly)!=4 or not cv2.isContourConvex(poly):
            # Rounded openings and their wall shadows need not form a four-point
            # contour. A fragmented opening may fill only half of its box;
            # this rectangle only seeds the separately verified model.
            box=cv2.boxPoints(cv2.minAreaRect(contour))
            if cv2.contourArea(contour)<.50*cv2.contourArea(box):continue
            poly=box
        q=ordered_quad(poly)
        if not quad_inside_mask(q,allowed):continue
        xy=world_xy(q,profile,z);sides=np.linalg.norm(np.roll(xy,-1,axis=0)-xy,axis=1)
        if np.max(abs(np.sort(sides)-np.sort(np.tile(hole_size,2)))/np.sort(np.tile(hole_size,2)))>.30:continue
        center=xy.mean(0)
        if any(np.linalg.norm(center-s[0])<4 for s in seeds):continue
        axis=xy[1]-xy[0];angle=np.arctan2(axis[1],axis[0])
        # Select which model side corresponds to this opening side.
        if abs(sides[0]-hole_size[1])<abs(sides[0]-hole_size[0]):angle-=np.pi/2
        seeds.append((center,angle))
    if not seeds:return []
    # Distance and orientation support reject unrelated nearby texture edges.
    distance=evidence.distance(enhanced=True)
    gx=cv2.Sobel(gray,cv2.CV_32F,1,0);gy=cv2.Sobel(gray,cv2.CV_32F,0,1)
    magnitude=np.hypot(gx,gy);gx/=np.maximum(magnitude,1);gy/=np.maximum(magnitude,1)
    def rectangle(w,h,center=(0,0)):
        return np.array([[-w,-h],[w,-h],[w,h],[-w,h]],float)/2+center
    outer=rectangle(width,height);offset=(np.asarray(hole['center'])-.5)*[width,height]
    inner=rectangle(*hole_size,offset)
    # Sample away from raised corner posts; the model's broad rim is lower.
    along=np.linspace(.17,.83,24)
    sample=np.concatenate([a+(b-a)*along[:,None] for poly in (outer,inner) for a,b in zip(poly,np.roll(poly,-1,axis=0))])
    T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));K=np.asarray(profile['intrinsics']['K'])
    def project(points):
        shape=points.shape;xy=points.reshape(-1,2);world=np.c_[xy,np.full(len(xy),z)]
        camera=world@T[:3,:3].T+T[:3,3];uv=camera@K.T
        return (uv[:,:2]/uv[:,2:]).reshape(shape)
    def evaluate(poses,strict=True):
        a=poses[:,2];R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]).transpose(2,0,1)
        points=np.einsum('pj,nij->npi',sample,R)+poses[:,None,:2]
        uv=project(points);pixels=np.rint(uv).astype(int);h,w=gray.shape
        inside=(pixels[:,:,0]>=0)&(pixels[:,:,0]<w)&(pixels[:,:,1]>=0)&(pixels[:,:,1]<h)
        px=np.clip(pixels[:,:,0],0,w-1);py=np.clip(pixels[:,:,1],0,h-1)
        inside &=allowed[py,px]>0
        d=distance[py,px]
        lines=uv.reshape(-1,8,24,2);tangent=lines[:,:,-1]-lines[:,:,0];tangent/=np.maximum(np.linalg.norm(tangent,axis=2,keepdims=True),1e-6)
        normal=np.repeat(np.stack([-tangent[:,:,1],tangent[:,:,0]],axis=2),24,axis=1)
        alignment=np.abs(gx[py,px]*normal[:,:,0]+gy[py,px]*normal[:,:,1])
        # Small gradients may sit one pixel off the edge after remapping.
        support=((d<2.5)&inside).reshape(-1,8,24).mean(2)
        cost=(np.minimum(d,5)/5+.20*(1-alignment)).reshape(-1,8,24).mean(2)
        outer_support=support[:,:4]
        regular=(outer_support.min(1)>=.40)&(outer_support.mean(1)>=.65)
        # A white rim on a white desk can lose part of one side. Require
        # three especially strong sides before accepting that weaker fourth.
        faint_side=(outer_support.min(1)>=.25)&(np.sort(outer_support,axis=1)[:,1]>=.85)&(outer_support.mean(1)>=.80)
        valid=(regular|faint_side)&(np.sort(support[:,4:],axis=1)[:,1]>=.55)&inside.all(1)
        score=.65*cost[:,:4].mean(1)+.35*cost[:,4:].mean(1)
        # Limit average contour error as well as coverage: dense background
        # texture can be near every model side without following its shape.
        contour_ok=score<=.18
        if strict and raised.ready and not np.any(valid&contour_ok):
            # A faint fourth rim can dominate the average despite three clear
            # sides and an opening. Four independent, height-corrected corner
            # pairs may corroborate that fit; they never replace rim/ROI/hole
            # evidence or establish a pallet by themselves.
            recover=valid&~contour_ok&(score<=.22)&faint_side&(np.sort(support[:,4:],axis=1)[:,1]>=.75)
            indices=np.flatnonzero(recover)
            if len(indices):
                corners,_=raised.evaluate(poses[indices])
                joint_cost=.8*score[indices]+.2*(1-corners.mean(1))
                contour_ok[indices]=(corners.min(1)>=.75)&(corners.mean(1)>=.85)&(joint_cost<=.18)
        valid &=contour_ok
        if not strict:valid=inside.all(1)
        score[~valid]=np.inf
        return score,support
    from .raised_model import RaisedEvidence
    raised=RaisedEvidence(frame,mesh,allowed,profile,evidence=evidence)
    def ranked(scores,poses):
        if not raised.ready:return scores
        support,verified=raised.evaluate(poses)
        # A small tie-break from the fixed raised corners; all independent
        # rim, opening, ROI and size checks remain mandatory.
        return scores-.025*support.mean(1)*verified
    proposals=[]
    # Bounded local search: no global rotation/scale sweep on the Pi.
    for center,angle in seeds[:8]:
        c,s=np.cos(angle),np.sin(angle);R=np.array([[c,-s],[s,c]])
        center=center-offset@R.T
        grid=np.stack(np.meshgrid(np.arange(-6,6.1,2),np.arange(-6,6.1,2),np.deg2rad(np.arange(-8,8.1,2)),indexing='ij'),axis=-1).reshape(-1,3)
        poses=grid+np.r_[center,angle];scores,support=evaluate(poses,strict=False)
        if not np.isfinite(scores).any():continue
        best=poses[np.argmin(ranked(scores,poses))]
        fine=np.stack(np.meshgrid(np.arange(-1.5,1.6,.5),np.arange(-1.5,1.6,.5),np.deg2rad(np.arange(-1.5,1.6,.5)),indexing='ij'),axis=-1).reshape(-1,3)
        poses=fine+best;scores,support=evaluate(poses);i=int(np.argmin(ranked(scores,poses)))
        if not np.isfinite(scores[i]):continue
        cx,cy,a=poses[i];R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
        q=ordered_quad(project_plane(outer@R.T+[cx,cy],profile,z))
        if not quad_inside_mask(q,allowed):continue
        if any(np.linalg.norm(q.mean(0)-p['quad'].mean(0))<8 for p in proposals):continue
        proposals.append({'quad':q,'edge_support':support[i].tolist(),'edge_cost':float(scores[i]),
                          'raised_edge_recovery':bool(scores[i]>.18)})
    return proposals
