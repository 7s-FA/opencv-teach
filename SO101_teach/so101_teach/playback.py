"""Time-based preview transport. No dependency on motor state."""
import time
from .joint_path import JointPath
class Playback:
    def __init__(self):self.targets=[];self.start=None;self.elapsed=0.;self.last=None;self.speed=1.;self.paused=False;self.path=None
    def begin(self,start,targets,now=None,*,rate=None):self.start=dict(start);self.targets=[dict(t) for t in targets];self.elapsed=0.;self.last=time.monotonic() if now is None else now;self.paused=False;self.path=JointPath(self.start,self.targets,rate)
    @property
    def duration(self):return self.path.duration if self.path else 0.
    def seek(self,seconds):self.elapsed=max(0.,min(self.duration,float(seconds)));self.last=time.monotonic()
    def sample(self,now=None):
        now=time.monotonic() if now is None else now
        if self.last is not None and not self.paused:self.elapsed=min(self.duration,self.elapsed+max(0,now-self.last)*self.speed)
        self.last=now
        if not self.targets:return self.start,True
        return self.path.sample(self.elapsed),self.elapsed>=self.duration
