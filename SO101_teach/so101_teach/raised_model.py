"""Optional height-aware evidence from four pairs of raised pallet ridge edges."""
import cv2
import numpy as np
from .height_reference import detection_plane_z


def ridge_edges(tri,rim):
    """Recognize four corner L ridges, without treating slopes as flat tops."""
    low=tri.min((0,1));high=tri.max((0,1));size=high-low
    if not .5<high[2]-rim<min(size[:2])*.25:return []
    edges=np.concatenate([tri[:,[0,1]],tri[:,[1,2]],tri[:,[2,0]]])
    edges=edges[np.all(abs(edges[:,:,2]-high[2])<.001,axis=1)]
    if not len(edges):return []
    edges=np.sort(edges,axis=1);edges=np.unique(np.round(edges,5),axis=0)
    length=np.linalg.norm(edges[:,1]-edges[:,0],axis=1)
    edges=edges[(length>5)&(length<max(size[:2])*.45)]
    if len(edges)!=8:return []
    middle=edges.mean(1)[:,:2]-(low[:2]+high[:2])/2
    groups=(middle[:,0]>0).astype(int)*2+(middle[:,1]>0)
    if not np.array_equal(np.bincount(groups,minlength=4),[2,2,2,2]):return []
    for group in range(4):
        pair=edges[groups==group];direction=pair[:,1,:2]-pair[:,0,:2]
        if abs(np.dot(*direction))/(np.linalg.norm(direction[0])*np.linalg.norm(direction[1]))>.05:return []
    return edges[np.argsort(groups)].tolist()


class RaisedEvidence:
    def __init__(self,frame,mesh,allowed,profile,*,evidence=None):
        self.ready=False
        edges=mesh.get('raised_edges_mm',[])
        if mesh.get('shape','ring')!='ring' or len(edges)!=8 or not profile or not profile.get('extrinsics') or not profile.get('intrinsics'):return
        edges=np.asarray(edges,float);self.mesh=mesh;self.allowed=allowed;self.profile=profile
        middle=np.asarray(mesh['low_mm'][:2])+np.asarray(mesh['size_mm'][:2])/2
        edges[:,:,:2]-=middle
        self.points=(edges[:,0,None]+(edges[:,1]-edges[:,0])[:,None]*np.linspace(.12,.88,12)[None,:,None]).reshape(-1,3)
        self.points[:,2]+=detection_plane_z(profile,mesh)-mesh['rim_z_mm']
        from .frame_evidence import RegionEvidence
        evidence=evidence or RegionEvidence(frame,allowed)
        self.distance=evidence.distance(enhanced=True)
        self.T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera']));self.K=np.asarray(profile['intrinsics']['K']);self.ready=True
    def evaluate(self,poses):
        angles=poses[:,2];rotations=np.array([[np.cos(angles),-np.sin(angles)],[np.sin(angles),np.cos(angles)]]).transpose(2,0,1)
        xy=np.einsum('pj,nij->npi',self.points[:,:2],rotations)+poses[:,None,:2]
        world=np.concatenate([xy,np.broadcast_to(self.points[:,2],xy.shape[:2])[...,None]],axis=2)
        camera=world@self.T[:3,:3].T+self.T[:3,3];uv=camera@self.K.T;uv=uv[:,:,:2]/uv[:,:,2:]
        pixel=np.rint(uv).astype(int);h,w=self.allowed.shape
        inside=(camera[:,:,2]>0)&(pixel[:,:,0]>=0)&(pixel[:,:,0]<w)&(pixel[:,:,1]>=0)&(pixel[:,:,1]<h)
        px=np.clip(pixel[:,:,0],0,w-1);py=np.clip(pixel[:,:,1],0,h-1);inside &=self.allowed[py,px]>0
        support=((self.distance[py,px]<2.5)&inside).reshape(-1,4,2,12).mean(3).min(2)
        verified=(np.count_nonzero(support>=.55,axis=1)>=3)&(support.mean(1)>=.60)
        return support,verified


def attach_raised(frame,mesh,allowed,profile,result,*,evidence=None):
    evidence=RaisedEvidence(frame,mesh,allowed,profile,evidence=evidence)
    if not evidence.ready:return result
    seen=set()
    for c in [*result.get('candidates',[]),result.get('selected')]:
        if c is None or id(c) in seen or not c.get('shape_match') or not c.get('metric'):continue
        seen.add(id(c));metric=c['metric'];pose=np.array([[*metric['center_xy_mm'],np.deg2rad(metric['yaw_deg'])]])
        support,verified=evidence.evaluate(pose)
        c.update(raised_corner_support=support[0].tolist(),raised_features_verified=bool(verified[0]))
    return result
