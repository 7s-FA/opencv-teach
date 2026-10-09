"""Collect every independent observation in a bounded acquisition window."""
from copy import deepcopy
import math
import numpy as np

ACQUISITION_SECONDS=3.
ACQUISITION_RANGE=(3.,10.)
ATTEMPT_RANGE=(1,5)
HOLD_RANGE=(1.,10.)

def checked_seconds(value,bounds,label):
    value=float(value)
    if not math.isfinite(value) or not bounds[0]<=value<=bounds[1]:
        raise ValueError(f'{label}은 {bounds[0]:g}~{bounds[1]:g}초입니다.')
    return value

def saved_seconds(value,bounds,default):
    """Migrate old saved limits; new edits are rejected rather than clamped."""
    try:value=float(value)
    except (TypeError,ValueError):return default
    return max(bounds[0],min(bounds[1],value)) if math.isfinite(value) else default

def angle_delta(a,b,period=90):return abs((a-b+period/2)%period-period/2)

def agrees(a,b):
    period=a.get('symmetry_deg',90)
    return (period==b.get('symmetry_deg',90)
            and a.get('metric',{}).get('mesh_yaw_offset_deg',0.)==b.get('metric',{}).get('mesh_yaw_offset_deg',0.)
            and np.linalg.norm(np.asarray(a['center_px'])-b['center_px'])<=5
            and angle_delta(a['image_angle_deg'],b['image_angle_deg'],period)<=3
            and max(a['area_px'],b['area_px'])/min(a['area_px'],b['area_px'])<=1.12)

def checked_attempts(value):
    number=checked_seconds(value,ATTEMPT_RANGE,'최대 측정 횟수')
    if not number.is_integer():raise ValueError('최대 측정 횟수는 1~5회 정수입니다.')
    return int(number)

def dominant_group(candidates):
    """Largest pairwise-agreeing strict majority, with a graph coloring bound.

    Coloring avoids enumerating every subset as the all-frame window grows.
    A chain of nearby positions still cannot masquerade as a single pose.
    """
    n=len(candidates);minimum=max(2,n//2+1)
    if n<2:return []
    adjacent=[0]*n
    for i in range(n):
        for j in range(i):
            if agrees(candidates[i],candidates[j]):adjacent[i]|=1<<j;adjacent[j]|=1<<i
    best=[]
    def colors(pool):
        order=[];bounds=[];color=0
        while pool:
            color+=1;available=pool
            while available:
                bit=available&-available;v=bit.bit_length()-1
                order.append(v);bounds.append(color);pool^=bit
                available &=~(bit|adjacent[v])
        return order,bounds
    def expand(group,pool):
        nonlocal best
        order,bounds=colors(pool)
        for i in range(len(order)-1,-1,-1):
            if len(group)+bounds[i]<max(minimum,len(best)+1):return
            v=order[i];next_group=group+[v];remaining=pool&adjacent[v]
            if not remaining:
                if len(next_group)>len(best):best=next_group
            else:expand(next_group,remaining)
            pool &=~(1<<v)
    expand([], (1<<n)-1)
    return [candidates[i] for i in sorted(best)] if len(best)>=minimum else []

def wrapped_median(values,period):
    base=values[0];return float(np.median(base+(np.asarray(values)-base+period/2)%period-period/2)%period)

def median_candidate(group,profile=None,mesh=None):
    center=np.median([c['center_px'] for c in group],axis=0)
    ref=min(group,key=lambda c:np.linalg.norm(np.asarray(c['center_px'])-center));out=deepcopy(ref)
    period=ref.get('symmetry_deg',90);yaw=wrapped_median([c['metric']['yaw_deg'] for c in group],period)
    xy=np.median([c['metric']['center_xy_mm'] for c in group],axis=0)
    out['metric'].update(center_xy_mm=xy.tolist(),yaw_deg=yaw,aggregation='temporal_median',verified=False)
    if profile and mesh and profile.get('intrinsics') and profile.get('extrinsics'):
        from .vision import world_xy,project_plane,projected_jig_axes,rectangle_quality
        from .height_reference import detection_plane_z
        import cv2
        z=detection_plane_z(profile,mesh);quads=[world_xy(c['quad'],profile,z) for c in group]
        reference=world_xy(ref['quad'],profile,z)
        areas=[abs(cv2.contourArea(np.float32(q))) for q in quads]
        scale=math.sqrt(float(np.median(areas))/max(abs(cv2.contourArea(np.float32(reference))),1e-9))
        delta=math.radians((yaw-ref['metric']['yaw_deg']+period/2)%period-period/2)
        rotation=np.array([[math.cos(delta),-math.sin(delta)],[math.sin(delta),math.cos(delta)]])
        world=(reference-ref['metric']['center_xy_mm'])@rotation.T*scale+xy
        quad=project_plane(world,profile,z);out['quad']=quad.tolist();out['center_px']=project_plane([xy],profile,z)[0].tolist()
        outline=np.concatenate([a+(b-a)*np.linspace(0,1,24,endpoint=False)[:,None] for a,b in zip(world,np.roll(world,-1,axis=0))])
        out['outline_px']=project_plane(outline,profile,z).tolist()
        edge=quad[1]-quad[0];out['image_angle_deg']=float(np.degrees(np.arctan2(edge[1],edge[0]))%period)
        out['area_px']=float(abs(cv2.contourArea(np.float32(out['outline_px']))))
        out['metric']['sides_mm']=np.linalg.norm(world-np.roll(world,1,axis=0),axis=1).tolist()
        out['outer_shape']={**out.get('outer_shape',{}),**rectangle_quality(world,mesh['size_mm'][:2])}
        out['axes_px']=projected_jig_axes(out,profile,z)
    else:
        # Geometry-free fixtures/manual observations still get a true median,
        # with their outline and centre transformed together.
        angle=wrapped_median([c['image_angle_deg'] for c in group],period)
        delta=math.radians((angle-ref['image_angle_deg']+period/2)%period-period/2)
        rotation=np.array([[math.cos(delta),-math.sin(delta)],[math.sin(delta),math.cos(delta)]])
        scale=math.sqrt(float(np.median([c['area_px'] for c in group]))/ref['area_px'])
        for key in ('quad','outline_px','axes_px'):
            if key in out:out[key]=((np.asarray(ref[key])-ref['center_px'])@rotation.T*scale+center).tolist()
        out.update(center_px=center.tolist(),image_angle_deg=angle,area_px=float(np.median([c['area_px'] for c in group])))
    out.update(temporal_confirmed=True,consensus_median=True,shape_match=all(c.get('shape_match') for c in group))
    if not out['shape_match']:out['temporal_match']=True
    return out

class StableCandidate:
    """All distinct processed frames count; only a finished window may adopt."""
    angle_delta=staticmethod(angle_delta)
    def __init__(self,seconds=ACQUISITION_SECONDS,attempts_limit=3):self.configure(seconds,attempts_limit)
    def configure(self,seconds,attempts_limit=None):
        seconds=checked_seconds(seconds,ACQUISITION_RANGE,'지그 획득 시간')
        attempts=checked_attempts(attempts_limit if attempts_limit is not None else getattr(self,'attempts_limit',3))
        self.seconds=seconds;self.attempts_limit=attempts;self.clear()
    def clear(self,started=None):
        self.started=started;self.last_at=None;self.samples=[];self.latest=None;self.completed_attempts=0
        self.profile=None;self.mesh=None;self.last_output=None;self.exhausted=False
    def _progress(self,now):
        out=deepcopy(self.latest or {'candidates':[]});out['selected']=None
        out.update(status='confirming',stable_candidate_seconds=max(0.,min(now-self.started,self.seconds)),
                   stable_candidate_required_seconds=self.seconds,stable_candidate_samples=len(self.samples),
                   stable_candidate_required_samples=2,acquisition_seconds=self.seconds,
                   acquisition_started_at=self.started,acquisition_attempt=self.completed_attempts+1,
                   acquisition_completed_attempts=self.completed_attempts,acquisition_attempts_limit=self.attempts_limit)
        return out
    @staticmethod
    def candidate(result):
        if result.get('status') in ('ambiguous','orientation_unconfirmed','calibration_mismatch','settings_changed','error'):return None
        candidates=[c for c in result.get('candidates',[]) if c.get('metric') and c.get('outer_shape',{}).get('valid') and c.get('area_px',0)>0]
        current=result.get('selected')
        if current not in candidates:current=None
        if current is None and candidates and (len(candidates)==1 or candidates[0]['score']-candidates[1]['score']>=.08):current=candidates[0]
        return current
    def finish(self,now):
        out=None
        while self.started is not None and now>=self.started+self.seconds:
            out=self._finish_window()
        return out
    def _finish_window(self):
        completed=self.started+self.seconds
        out=self._progress(completed);values=[c for _,c in self.samples];group=dominant_group(values)
        latest=self.candidate(self.latest or {})
        enough=len(group)>=2 and (all(c.get('shape_match') for c in group) or len(group)>=3)
        out.update(acquisition_completed_at=completed,acquisition_completed_attempts=self.completed_attempts+1,
                   adoption_inliers=len(group),adoption_rejected=len(values)-len(group),status='acquisition_failed')
        if enough and latest and all(agrees(latest,c) for c in group):
            chosen=median_candidate(group,self.profile,self.mesh)
            out.update(selected=chosen,candidates=[chosen],status='shape_match' if chosen['shape_match'] else 'stable_candidate',
                       verified_for_motion=False,pose_measured_at=self.last_at)
        else:
            out['acquisition_issue']='관측 부족' if len(values)<2 else '위치 불일치' if not enough else '최신 위치 미확인'
        self.completed_attempts+=1;self.started=completed;self.samples=[];self.latest=None
        self.exhausted=not out.get('selected') and self.completed_attempts>=self.attempts_limit
        out['acquisition_exhausted']=self.exhausted
        if self.exhausted:self.started=None
        elif out.get('selected'):self.completed_attempts=0
        self.last_output=deepcopy(out)
        return out
    def update(self,result,now,*,profile=None,mesh=None):
        if not math.isfinite(now):self.clear();return {**deepcopy(result),'selected':None}
        if self.last_at is not None and now<self.last_at:self.clear()
        if self.exhausted:return deepcopy(self.last_output)
        if self.started is None:self.started=now
        self.profile=profile;self.mesh=mesh
        # Close before adding a frame completed beyond this window's deadline.
        done=self.finish(now) if now>self.started+self.seconds else None
        if self.exhausted:return deepcopy(done)
        fresh=self.last_at is None or now>self.last_at
        if fresh:
            self.latest=deepcopy(result);self.last_at=now
            current=self.candidate(result)
            if current is not None:self.samples.append((now,deepcopy(current)))
        if done is not None:
            # A closing frame outside the window is not another vote, but it
            # must not contradict a pose we are about to publish for the first time.
            current=self.candidate(result)
            if done.get('selected') and (current is None or not agrees(current,done['selected'])):
                done.update(selected=None,status='acquisition_failed',acquisition_issue='최신 위치 불일치')
                self.completed_attempts=done['acquisition_completed_attempts']
                self.exhausted=self.completed_attempts>=self.attempts_limit
                done['acquisition_exhausted']=self.exhausted
                if self.exhausted:self.started=None;self.samples=[];self.latest=None
                self.last_output=deepcopy(done)
            return done
        done=self.finish(now)
        self.last_output=deepcopy(done if done is not None else self._progress(now))
        return deepcopy(self.last_output)
