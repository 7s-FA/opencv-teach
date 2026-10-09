"""Short visual transitions only; never used as teaching or motion measurements."""
from copy import deepcopy
import time
import numpy as np

class DisplayPoses:
    def __init__(self,duration=.35):self.duration=duration;self.states={}
    def clear(self):self.states.clear()
    @staticmethod
    def signature(item):
        m=item.get('metric') or {}
        return (tuple(item['center_px']),item['image_angle_deg'],tuple(map(tuple,item.get('quad',()))),
                tuple(m.get('center_xy_mm',())),m.get('yaw_deg'),m.get('symmetry_deg',item.get('symmetry_deg',90)))
    @staticmethod
    def angle(a,b,f,period):return a+((b-a+period/2)%period-period/2)*f
    def sample(self,state,now):
        before,target,began,_=state;f=max(0.,min(1.,(now-began)/self.duration)) if self.duration else 1.
        moving=f<1
        f=f*f*(3-2*f);out=deepcopy(target)
        def mix(a,b):return (np.asarray(a,float)*(1-f)+np.asarray(b,float)*f).tolist()
        out['center_px']=mix(before['center_px'],target['center_px'])
        if before.get('quad') and target.get('quad'):
            a=np.asarray(before['quad']);b=np.asarray(target['quad']);shift=min(range(4),key=lambda k:np.sum((a-np.roll(b,k,axis=0))**2));b=np.roll(b,shift,axis=0)
            out['quad']=mix(a,b)
            if before.get('outline_px') and target.get('outline_px') and len(before['outline_px'])==len(target['outline_px']):
                border=np.roll(np.asarray(target['outline_px']),shift*(len(target['outline_px'])//4),axis=0)
                out['outline_px']=mix(before['outline_px'],border)
            elif moving:out.pop('outline_px',None)
        period=target.get('symmetry_deg',90)
        out['image_angle_deg']=self.angle(before['image_angle_deg'],target['image_angle_deg'],f,period)
        if before.get('metric') and target.get('metric'):
            m=out['metric'];m['center_xy_mm']=mix(before['metric']['center_xy_mm'],m['center_xy_mm'])
            m['yaw_deg']=self.angle(before['metric']['yaw_deg'],m['yaw_deg'],f,m.get('symmetry_deg',period))
        out.pop('axes_px',None);out['display_transition']=moving
        return out
    def update(self,key,item,now=None,frozen=False):
        now=time.monotonic() if now is None else now
        if item is None:return None
        state=self.states.get(key)
        if state is None or now-state[3]>3 or bool(state[1].get('metric'))!=bool(item.get('metric')):
            saved=deepcopy(item);self.states[key]=(saved,saved,now-self.duration,now);return deepcopy(item)
        if frozen:
            state=(self.sample(state,now),deepcopy(item),now-self.duration,now);self.states[key]=state
            return self.sample(state,now)
        if self.signature(item)!=self.signature(state[1]):
            state=(self.sample(state,now),deepcopy(item),now,now)
        else:state=(state[0],deepcopy(item),state[2],now)
        self.states[key]=state
        return self.sample(state,now)
