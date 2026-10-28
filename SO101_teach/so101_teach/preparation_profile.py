"""Bounded, in-memory preparation measurements; no device I/O or decisions."""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import math
import threading
import time
import uuid


class PreparationProfile:
    def __init__(self,clock=None):
        self.clock=clock or time.monotonic;self.started=self.clock()
        self.lock=threading.RLock();self.metrics={};self.milestones=OrderedDict();self.seen=OrderedDict()
        self.profile_id=uuid.uuid4().hex;self.interval_seq=0;self.intervals=[];self.dropped_intervals=0
        self.sample_tokens_evicted=0

    def add(self,name,seconds):
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or seconds<0:return
        with self.lock:
            if name not in self.metrics and len(self.metrics)>=256:return
            row=self.metrics.setdefault(name,{'count':0,'total_seconds':0.,'max_seconds':0.})
            row['count']+=1;row['total_seconds']+=seconds;row['max_seconds']=max(row['max_seconds'],seconds)

    @contextmanager
    def measure(self,name):
        began=self.clock()
        try:yield
        finally:
            ended=self.clock();self.add(name,max(0.,ended-began))
            with self.lock:
                if name in self.metrics:
                    row=self.metrics[name]
                    first=round(max(0.,began-self.started),6);last=round(max(0.,ended-self.started),6)
                    row['first_started_seconds']=min(first,row.get('first_started_seconds',first))
                    row['last_finished_seconds']=max(last,row.get('last_finished_seconds',last))
                    row['interval_count']=row.get('interval_count',0)+1
                    self.interval_seq+=1
                    self.intervals.append({'interval_id':self.interval_seq,'name':name,
                                           'started_seconds':first,'finished_seconds':last})
                    if len(self.intervals)>4096:
                        self.intervals.pop(0);self.dropped_intervals+=1

    def call(self,name,function,*args,**kwargs):
        with self.measure(name):return function(*args,**kwargs)

    def mark(self,name):
        with self.lock:
            self.milestones[name]=round(max(0.,self.clock()-self.started),6)
            if len(self.milestones)>256:self.milestones.popitem(last=False)

    def sample(self,source,token,values):
        """Count each consumed frame once, even when many polls return it."""
        if token is None:return
        with self.lock:
            tokens=self.seen.setdefault(source,OrderedDict())
            if token in tokens:return
            tokens[token]=None;self.seen.move_to_end(source)
            if len(tokens)>256:tokens.popitem(last=False);self.sample_tokens_evicted+=1
            if len(self.seen)>128:
                self.sample_tokens_evicted+=len(self.seen.popitem(last=False)[1])
            for name,seconds in values.items():self.add(source+'.'+name,seconds)

    def snapshot(self):
        with self.lock:
            metrics=deepcopy(self.metrics)
            for row in metrics.values():
                row['mean_seconds']=round(row['total_seconds']/row['count'],6)
                row['without_interval_count']=row['count']-row.get('interval_count',0)
                for name in ('total_seconds','max_seconds'):row[name]=round(row[name],6)
            return {'schema':2,'profile_id':self.profile_id,'snapshot_kind':'cumulative',
                    'origin_monotonic_s':self.started,
                    'elapsed_seconds':round(max(0.,self.clock()-self.started),6),
                    'metrics':metrics,'milestones':dict(self.milestones),
                    'intervals':list(self.intervals),'dropped_intervals':self.dropped_intervals,
                    'sample_tokens_evicted':self.sample_tokens_evicted,
                    'aggregation':'replace earlier snapshot of same profile_id; do not sum cumulative reports',
                    'note':'병렬·포함 구간은 중첩되므로 합산하지 않음. 검출 시간은 소비한 고유 프레임만 집계.'}


def timed(measurements,name,function,*args,**kwargs):
    return function(*args,**kwargs) if measurements is None else measurements.call(name,function,*args,**kwargs)
