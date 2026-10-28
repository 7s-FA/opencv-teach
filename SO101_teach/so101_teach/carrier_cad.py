"""Independent carrier heading from visible fixed CAD edges, on the current image.

Immutable mesh features are cached; image evidence and visibility are always current.
No compiler, native extension, network or device access is needed.
"""
from pathlib import Path
from functools import lru_cache
from copy import deepcopy
import hashlib
import re
import cv2
import numpy as np
from .pose_refinement import sample
from .height_reference import detection_plane_z

def _read_triangles(mesh):
    raw=Path(mesh['stl_path']).read_bytes();unit=mesh.get('unit','mm')
    if unit not in ('mm','cm','m'):raise ValueError('STL 단위 오류')
    identity=hashlib.sha256(raw if unit=='mm' else raw+unit.encode()).hexdigest()
    if identity!=mesh['sha256']:raise ValueError('CAD 방향 기준 STL 지문 불일치')
    count=int.from_bytes(raw[80:84],'little') if len(raw)>=84 else 0
    if count and len(raw)==84+count*50:
        tri=np.frombuffer(raw[84:],dtype=np.dtype([('normal','<f4',3),('v','<f4',(3,3)),('attr','<u2')]))['v'].astype(float)
    else:
        vertices=re.findall(r'\bvertex\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)',raw.decode('ascii'))
        if not vertices or len(vertices)%3:raise ValueError('STL 삼각형 형식 오류')
        tri=np.asarray(vertices,float).reshape(-1,3,3)
    tri*=dict(mm=1,cm=10,m=1000)[unit]
    if not np.isfinite(tri).all():raise ValueError('STL 좌표 오류')
    return tri

@lru_cache(maxsize=4)
def _cached_features(path,mtime,size,identity,unit,rim,centers):
    mesh={'stl_path':path,'sha256':identity,'unit':unit,'rim_z_mm':rim,
          'assembly':{'fixtures':[{'center_mm':c} for c in centers]}}
    return _mesh_features(mesh)

def mesh_features(mesh):
    path=Path(mesh['stl_path']).resolve();stat=path.stat()
    centers=tuple(tuple(f['center_mm']) for f in mesh['assembly']['fixtures'])
    if len(centers)!=6:raise ValueError('고정 지그6개 형상 기준이 필요합니다.')
    return _cached_features(str(path),stat.st_mtime_ns,stat.st_size,mesh['sha256'],mesh.get('unit','mm'),mesh['rim_z_mm'],centers)

def _mesh_features(mesh):
 tri=_read_triangles(mesh)
 vertices,inv=np.unique(np.round(tri.reshape(-1,3),5),axis=0,return_inverse=True);faces=inv.reshape(-1,3)
 normals=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]);normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-12)
 edges=np.sort(np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]]),axis=1);ids=np.tile(np.arange(len(faces)),3);order=np.lexsort((edges[:,1],edges[:,0]));edges=edges[order];ids=ids[order];starts=np.r_[0,1+np.flatnonzero(np.any(edges[1:]!=edges[:-1],axis=1)),len(edges)]
 points=[];groups=[]
 centers=np.array([f['center_mm'] for f in mesh['assembly']['fixtures']])
 for lo,hi in zip(starts[:-1],starts[1:]):
  adjacent=ids[lo:hi]
  if len(adjacent)==2 and abs(normals[adjacent[0]]@normals[adjacent[1]])>np.cos(np.deg2rad(25)):continue
  a,b=vertices[edges[lo]];mid=(a+b)/2
  if min(a[2],b[2])<mesh['rim_z_mm']+.2:continue
  length=np.linalg.norm(b-a);n=max(2,int(np.ceil(length/.6)));t=(np.arange(n)+.5)/n
  points.append(a+(b-a)*t[:,None]);groups.extend([int(np.argmin(np.linalg.norm(centers-mid[:2],axis=1)))]*n)
 vertices,faces=np.unique(tri.reshape(-1,3),axis=0,return_inverse=True)
 arrays=(vertices,faces.reshape(-1,3),np.concatenate(points),np.asarray(groups))
 for array in arrays:array.flags.writeable=False
 return arrays

def depth_at_pixels(uv,z,shape,pixels,chunk_rows=50000):
    """Rasterize only requested pixels, using the original perspective depth.

    Sorted query pixels let each triangle row skip every unused image column.
    Row batches bound temporary storage without changing triangle coverage.
    """
    uv=np.asarray(uv).reshape(-1,3,2);z=np.asarray(z).reshape(-1,3);h,w=shape
    depth=np.full(h*w,np.inf,np.float32)
    query=np.unique(pixels[:,1]*w+pixels[:,0])
    if not len(query):return depth.reshape(shape)
    low=np.maximum(np.floor(uv.min(1)).astype(np.int64),0)
    high=np.minimum(np.ceil(uv.max(1)).astype(np.int64),[w-1,h-1])
    width=high[:,0]-low[:,0]+1;height=high[:,1]-low[:,1]+1
    a=uv[:,1]-uv[:,0];b=uv[:,2]-uv[:,0];det=a[:,0]*b[:,1]-a[:,1]*b[:,0]
    ids=np.flatnonzero((width>0)&(height>0)&(abs(det)>=1e-6))
    counts=height[ids];cumulative=np.r_[0,np.cumsum(counts)];start=0
    while start<len(ids):
        stop=max(start+1,int(np.searchsorted(cumulative,cumulative[start]+chunk_rows,side='right')-1))
        stop=min(stop,len(ids));n=counts[start:stop]
        face=np.repeat(ids[start:stop],n)
        row=low[face,1]+np.arange(n.sum())-np.repeat(np.r_[0,np.cumsum(n)[:-1]],n)
        first=np.searchsorted(query,row*w+low[face,0])
        last=np.searchsorted(query,row*w+high[face,0],side='right')
        n=last-first;occupied=n>0
        if not np.any(occupied):
            start=stop
            continue
        n=n[occupied];face=np.repeat(face[occupied],n)
        indices=np.repeat(first[occupied],n)+np.arange(n.sum())-np.repeat(np.r_[0,np.cumsum(n)[:-1]],n)
        pixel=query[indices];xx=pixel%w;yy=pixel//w
        x=xx-uv[face,0,0];y=yy-uv[face,0,1]
        u=(x*b[face,1]-y*b[face,0])/det[face]
        v=(a[face,0]*y-a[face,1]*x)/det[face]
        inside=(u>=-.015)&(v>=-.015)&(u+v<=1.015)
        f=face[inside];u=u[inside];v=v[inside]
        zz=1/((1-u-v)/z[f,0]+u/z[f,1]+v/z[f,2])
        np.minimum.at(depth,pixel[inside],zz.astype(np.float32))
        start=stop
    return depth.reshape(shape)


def project(points,o,c,yaw):
 p=points.copy();a=np.deg2rad(yaw);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]);mid=np.asarray(o['mesh']['low_mm'][:2])+np.asarray(o['mesh']['size_mm'][:2])/2
 p[:,:2]=(p[:,:2]-mid)@R.T*c.get('boundary_scale',1)+c['metric']['center_xy_mm'];p[:,2]+=detection_plane_z(o['profile'],o['mesh'])-o['mesh']['rim_z_mm']
 T=np.linalg.inv(np.asarray(o['profile']['extrinsics']['base_from_camera']));cam=p@T[:3,:3].T+T[:3,3];uv,_=cv2.projectPoints(cam,np.zeros(3),np.zeros(3),np.asarray(o['profile']['intrinsics']['K']),np.asarray(o['profile']['intrinsics']['D']));return uv.reshape(-1,2),cam[:,2]

def evaluate(frame,mesh,allowed,profile,candidate):
    vertices,faces,points,groups=mesh_features(mesh)
    model=np.concatenate([vertices,points]);vertex_count=len(vertices)
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY);hsv=cv2.cvtColor(frame,cv2.COLOR_BGR2HSV)
    material=cv2.dilate(((hsv[:,:,1]>65)&(hsv[:,:,2]>25)).astype(np.uint8),np.ones((5,5),np.uint8))
    valid=((material==0)&(allowed>0)).astype(float)
    edges=(cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),15,45)>0)&(valid>0)
    distance=cv2.distanceTransform((~edges).astype(np.uint8),cv2.DIST_L2,3)
    h,w=allowed.shape;o={'mesh':mesh,'profile':profile};hypotheses=[]
    for turn in (0,180):
        yaw=(candidate['metric']['yaw_deg']+turn)%360
        projected,depth=project(model,o,candidate,yaw)
        uv=projected[vertex_count:];z=depth[vertex_count:]
        pixel=np.rint(uv).astype(int);inside=(pixel[:,0]>=0)&(pixel[:,0]<w)&(pixel[:,1]>=0)&(pixel[:,1]<h)
        pixel=np.clip(pixel,[0,0],[w-1,h-1])
        raster=depth_at_pixels(projected[:vertex_count][faces],depth[:vertex_count][faces],(h,w),pixel)
        visible=inside&(z<=raster[pixel[:,1],pixel[:,0]]+1.)
        # Hidden points cannot contribute to any alignment; drop them once.
        uv=uv[visible];group=groups[visible];best=None
        for dx in range(-3,4):
            shifts=np.array([[dx,dy] for dy in range(-3,4)])
            predicted=uv[None,:,:]+shifts[:,None,:];flat=predicted.reshape(-1,2)
            keep=(sample(valid,flat,0)>.99).reshape(7,-1)
            d=sample(distance,flat).reshape(7,-1)
            labels=np.broadcast_to(group,(7,len(group)))+np.arange(7)[:,None]*6
            count=np.bincount(labels[keep],minlength=42).reshape(7,6)
            weight=np.bincount(labels[keep],weights=np.exp(-d[keep]/1.5),minlength=42).reshape(7,6)
            support=np.bincount(labels[keep],weights=(d[keep]<2).astype(float),minlength=42).reshape(7,6)
            means=weight/np.maximum(count,1);supports=support/np.maximum(count,1)
            for i,shift in enumerate(shifts):
                eligible=count[i]>=30
                value=float(means[i,eligible].mean()) if eligible.sum()>=2 else 0.
                if best is None or value>best['score']:
                    best={'model_yaw':yaw,'teaching_heading':(yaw-180)%360,'score':value,'shift_px':shift.tolist(),
                          'groups':[{'count':int(count[i,k]),'score':float(means[i,k]),'support':float(supports[i,k])} for k in range(6)],
                          'visible_points':int(count[i].sum())}
        hypotheses.append(best)
    return hypotheses

def verdict(scores):
 order=np.argsort([s['score'] for s in scores]);best=scores[order[-1]];other=scores[order[0]];delta=np.array([a['score']-b['score'] for a,b in zip(best['groups'],other['groups'])]);counts=np.minimum([g['count'] for g in best['groups']],[g['count'] for g in other['groups']]);eligible=counts>=30;d=delta[eligible];leave=[float(np.mean(np.delete(d,i))) for i in range(len(d))] if len(d)>=3 else [0.];margin=best['score']-other['score'];ok=best['score']>=.55 and margin>=.08 and np.count_nonzero(d>=.05)>=3 and min(leave)>=.06
 return {'verified':bool(ok),'heading':best['teaching_heading'],'score':best['score'],'margin':margin,'independent_groups_margin_ge005':int(np.count_nonzero(d>=.05)),'min_leave_one_group_margin':min(leave)}


def resolve(frame,mesh,allowed,profile,candidate,junction):
    """Corroborate a junction, or require strong CAD evidence to replace it."""
    from .junction_orientation import evaluate as evaluate_junction
    scores=evaluate(frame,mesh,allowed,profile,candidate);decision=verdict(scores)
    difference=lambda a,b:abs((a-b+180)%360-180)
    if junction['verified'] and difference((junction['yaw_deg']-180)%360,decision['heading'])<1:
        if decision['margin']>=.08 and decision['independent_groups_margin_ge005']>=3 and decision['min_leave_one_group_margin']>=.06:
            return {**junction,'cad_direction_verification':decision,'orientation_corroborated_by_cad':True}
    if not decision['verified']:
        return {**junction,'verified':False,'reason':'CAD 고정 형상과 접합부 방향의 독립 확인이 부족합니다.','cad_direction_verification':decision}
    if junction['verified'] and difference((junction['yaw_deg']-180)%360,decision['heading'])>90:
        perturb=[(0,0,0,0),(-.1,0,0,0),(.1,0,0,0),(0,-.1,0,0),(0,.1,0,0),(0,0,-.15,0),(0,0,.15,0),(0,0,0,-.001),(0,0,0,.001)]
        stable=[]
        for dx,dy,da,ds in perturb:
            c=deepcopy(candidate);c['metric']['center_xy_mm']=(np.asarray(c['metric']['center_xy_mm'])+[dx,dy]).tolist()
            c['metric']['yaw_deg']+=da;c['boundary_scale']+=ds
            observed=evaluate_junction(frame,mesh,allowed,profile,c)
            stable.append(bool(observed['verified'] and difference(observed['yaw_deg'],junction['yaw_deg'])<1))
        decision['conflicting_junction_stability']=stable
        if all(stable):return {**junction,'verified':False,'reason':'독립 방향 근거가 서로 충돌합니다.','cad_direction_verification':decision}
    best=max(scores,key=lambda s:s['score'])
    return {'verified':True,'scores':scores,'yaw_deg':best['model_yaw'],'outline_px':[],
            'score':decision['score'],'margin':decision['margin'],'reason':'고정 CAD 형상 방향 확인',
            'method':'cad_fixed_features','cad_direction_verification':decision,'cad_direction_scores':scores}
