"""Local, bounded short-ramp waypoint interpolation shared by preview and motors.

Keep every nominal knot. Shared velocity and zero acceleration provide C2 joins.
Monotone tangent limits avoid joint overshoot; task-space paths are not straight.
"""
from dataclasses import dataclass
import math
GRIP_CHANGE=8
RAMP_FRACTION=.10

@dataclass
class Segment:
    start:dict
    goal:dict
    duration:float
    entry:dict
    exit:dict
    stop:bool
    def profile(self,name):
        # Only the first/last 10% changes velocity; 80% is constant speed.
        ramp=self.duration*RAMP_FRACTION
        cruise=((self.goal[name]-self.start[name])/self.duration
                -RAMP_FRACTION*(self.entry[name]+self.exit[name])/2)/(1-RAMP_FRACTION)
        return ramp,cruise
    @staticmethod
    def ramp_distance(v0,v1,seconds,ramp):
        u=seconds/ramp
        return v0*seconds+(v1-v0)*ramp*(u**3-.5*u**4)
    def sample(self,fraction):
        u=max(0.,min(1.,fraction))
        if u==0:return dict(self.start)
        if u==1:return dict(self.goal)
        seconds=u*self.duration;out={}
        for n,a in self.start.items():
            ramp,cruise=self.profile(n);v0=self.entry[n];v1=self.exit[n]
            first=ramp*(v0+cruise)/2
            if seconds<ramp:distance=self.ramp_distance(v0,cruise,seconds,ramp)
            elif seconds<=self.duration-ramp:distance=first+cruise*(seconds-ramp)
            else:distance=first+cruise*(self.duration-2*ramp)+self.ramp_distance(cruise,v1,seconds-(self.duration-ramp),ramp)
            out[n]=round(a+distance)
        return out
    def velocity(self,fraction):
        seconds=max(0.,min(1.,fraction))*self.duration;out={}
        for n in self.start:
            ramp,cruise=self.profile(n)
            if seconds<ramp:
                u=seconds/ramp;out[n]=self.entry[n]+(cruise-self.entry[n])*(3*u*u-2*u**3)
            elif seconds<=self.duration-ramp:out[n]=cruise
            else:
                u=(seconds-(self.duration-ramp))/ramp;out[n]=cruise+(self.exit[n]-cruise)*(3*u*u-2*u**3)
        return out

class JointPath:
    def __init__(self,start,targets,rate=None,*,first_stop=True,stop_flags=None):
        points=[dict(start),*[dict(t) for t in targets]];keys=list(start)
        if rate is not None and (not math.isfinite(rate) or rate<=0):raise ValueError('경로 속도 오류')
        if any(set(p)!=set(keys) for p in points):raise ValueError('경로 관절 구성 불일치')
        durations=[max(.02,max(abs(b[n]-a[n]) for n in keys)/((1-RAMP_FRACTION)*rate)) if rate else 2. for a,b in zip(points,points[1:])]
        stops=[];count=len(targets)
        if stop_flags is not None and len(stop_flags)!=count:raise ValueError("경로 정지점 구성 불일치")
        for i in range(count):
            before,at=points[i],points[i+1];after=points[i+2] if i+2<len(points) else at
            grip_change=any(abs(at.get('gripper',0)-p.get('gripper',0))>=GRIP_CHANGE for p in (before,after))
            stops.append(i==count-1 or first_stop and i==0 or grip_change or bool(stop_flags and stop_flags[i]))
        velocities=[dict.fromkeys(keys,0.) for _ in points]
        for k in range(1,len(points)-1):
            if stops[k-1]:continue
            for n in keys:
                if n=='gripper':continue
                left=(points[k][n]-points[k-1][n])/durations[k-1]
                right=(points[k+1][n]-points[k][n])/durations[k]
                if left*right>0:velocities[k][n]=math.copysign(min(abs(left),abs(right)),left)
            if not any(velocities[k].values()):stops[k-1]=True
        self.segments=[Segment(points[i],points[i+1],durations[i],velocities[i],velocities[i+1],stops[i]) for i in range(count)]
        self.duration=sum(durations)
    def sample(self,seconds):
        if not self.segments:return None
        remaining=max(0.,seconds)
        for segment in self.segments:
            if remaining<=segment.duration:return segment.sample(remaining/segment.duration)
            remaining-=segment.duration
        return dict(self.segments[-1].goal)
