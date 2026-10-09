"""Reject sustained high temperature, retaining the measured values for diagnostics."""
class TemperatureTracker:
    def __init__(self,limit=65,duration=5.):self.limit=limit;self.duration=duration;self.high={};self.last={}
    def observe(self,name,value,now):
        if not 0<=value<=255:raise RuntimeError(name+': 온도 읽기 오류')
        if name in self.last and now-self.last[name]>1.5:self.high.pop(name,None)
        self.last[name]=now
        if value<self.limit:self.high.pop(name,None);return
        since=self.high.setdefault(name,now)
        if now-since>=self.duration:raise RuntimeError(f'{name}: {self.limit}°C 이상 {self.duration:g}초 지속')

class VoltageTracker:
    """Debounce the servo's voltage alarm, clearing only on healthy full feedback."""
    def __init__(self,duration=5.):self.duration=duration;self.pending={};self.last={}
    def observe(self,name,active,now,*,complete=False):
        if name in self.last and now-self.last[name]>1.5:self.pending.pop(name,None)
        self.last[name]=now
        if active:
            first=name not in self.pending;since=self.pending.setdefault(name,now)
            if self.duration is not None and now-since>=self.duration:raise RuntimeError(f'{name}: 전압 경고 {self.duration:g}초 지속')
            return 'warning' if first else None
        if complete and name in self.pending:self.pending.pop(name);return 'recovered'


def monitor_tick(calibration,name,tick,*,verified=True):
    """Servo calibration angle, not the CAD frame's fixed rotation."""
    if type(tick) is not int:return '—'
    if not verified:return f'{tick} (영점 미확인)'
    mapping=calibration.angle_mapping
    angle=mapping.degrees(name,tick) if mapping else (tick-2047)*360/4096
    return f'{tick} ({angle:.1f}°)'


def monitor_load(raw):
    if type(raw) is not int or not 0<=raw<=2047:return '—'
    return f'{raw} ({(raw&1023)/1023*100:.1f}%)'
