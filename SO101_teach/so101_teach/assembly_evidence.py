"""Current-run inspection diagnostics, never a prerequisite for another run."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from .domain import atomic_json


def geometry_scope(placement):
    from .episode_inspection import criteria_signature
    stage=(placement or {}).get('linear_stage',{})
    geometry={key:stage.get(key) for key in ('position_mm','yaw_deg','endpoint_reference','alignment')}
    return {'criteria':criteria_signature(),'geometry':hashlib.sha256(json.dumps(geometry,sort_keys=True,allow_nan=False).encode()).hexdigest()}


def invalidate(path):
    path=Path(path)
    if path.exists():atomic_json(path,{'schema':1,'receipt':None,'invalidated_at':time.time()})


def invalidate_manual_motion(data_dir):
    """CLI jobs own this lock; a manual move outside them discards old evidence."""
    import fcntl
    data_dir=Path(data_dir);path=data_dir/'assembly-evidence.json'
    if not path.exists():return
    with (data_dir/'episode-cli.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        invalidate(path)


class AssemblyEvidence:
    def __init__(self,path=None,*,scope=None,episode_id=None):
        # Current-run diagnostics only. Never load prior results as prerequisites.
        self.path=Path(path) if path else None;self.scope=deepcopy(scope)
        self.episode_id=episode_id;self.stages={};self.pending=None;self.receipt=None

    @staticmethod
    def applies(check,product):
        return product=='B' and check.get('station')=='linear' and check.get('target')=='2'

    def record(self,step,product):
        check=step['inspection']
        if not self.applies(check,product):return
        expected=check['expected']
        item={'step_id':step['id'],'expected':expected,'at':time.time()}
        if expected=='empty':self.clear();return
        if expected=='housing_seated':
            self.stages={'housing_seated':item};self.pending=None;self.receipt=None
            if self.path:invalidate(self.path)
        elif expected=='insert_added' and 'housing_seated' in self.stages:
            self.stages['insert_added']=item;self.pending=None
        elif expected=='cap_added' and all(k in self.stages for k in ('housing_seated','insert_added')):
            self.pending={'product':'B','station':'linear','target':'2','episode_id':self.episode_id,
                          'scope':self.scope,'checks':[deepcopy(self.stages[k]) for k in ('housing_seated','insert_added')]+[item]}

    def commit(self):
        # Persist only when the episode, not merely its top check, has completed.
        if self.pending and self.path:
            self.receipt={**deepcopy(self.pending),'completed_at':time.time()}
            atomic_json(self.path,{'schema':1,'receipt':self.receipt})

    def clear_run(self):
        self.stages={};self.pending=None

    def clear(self):
        self.clear_run();self.receipt=None
        if self.path:invalidate(self.path)
