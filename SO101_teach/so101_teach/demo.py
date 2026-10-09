"""Explicit offline device simulator; never opens a serial port."""
import time
from .motion import MotionSession,MotionGate
from .domain import JOINTS,Snapshot
class DemoSession(MotionSession):
    def __init__(self,calibration,ticks):
        super().__init__('DEMO',calibration,bus_factory=lambda *a:None);self.values={n:{'Present_Position':v,'Goal_Position':v,'Torque_Enable':0,'Status':0,'Present_Temperature':25} for n,v in ticks.items()}
        self.motion_gate=MotionGate(calibration)
    def read(self,*args,**kw):
        if len(args)==3:return super().read(*args,**kw)
        reg,n=args
        if reg=='Internal_Goal_Position':return self.values[n]['Goal_Position']
        return self.values[n].get(reg,0)
    def write(self,*args,**kw):
        if len(args)==4:return super().write(*args,**kw)
        reg,n,v=args;self.values[n][reg]=v
        if reg=='Goal_Position':self.values[n]['Present_Position']=v
    def run(self):
        try:
            while not self.stop.is_set():
                health={n:{'torque':v['Torque_Enable'],'status':0,'temperature_c':25,'voltage_v':12.,'goal_ticks':v['Goal_Position'],'load_raw':0,'current_raw':0} for n,v in self.values.items()}
                self.latest=Snapshot('demo',{n:v['Present_Position'] for n,v in self.values.items()},health,time.monotonic(),time.time(),self.calibration.sha256,True,'DEMO')
                self.on_snapshot(self,self.latest);self.publish('sample',self.latest);self.stop.wait(.03)
        except Exception as exc:self.error=str(exc);self.publish('error',self.error)
        finally:
            self.before_close(self);self.running=False
