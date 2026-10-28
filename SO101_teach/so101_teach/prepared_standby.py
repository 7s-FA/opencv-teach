"""Finite resource retention; never a second motion or command dispatcher."""
from copy import deepcopy
import json
import math
import hashlib
import threading
import time
from pathlib import Path

SECONDS=180.


class Standby:
    def __init__(self):
        self.lock=threading.RLock();self.generation=0;self.active=False;self.until=None
        self.client=None;self.epoch=None;self.fence=None
    def begin(self,client=None,health=None):
        with self.lock:
            self.generation+=1;self.active=True;self.until=None
            self.client=client;self.epoch=(health or {}).get('epoch');self.fence=(health or {}).get('generation')
            return self.generation
    def complete(self,generation,now):
        with self.lock:
            if type(generation) is not int or generation!=self.generation or not self.active:raise ValueError('준비가 취소되거나 제어권이 변경되었습니다.')
            self.active=False;self.until=now+SECONDS
    def cancel(self):
        with self.lock:
            had=self.active or self.until is not None
            self.generation+=1;self.active=False;self.until=None
            return had
    def valid_client(self,health,now):
        if self.client is None:return True
        if not isinstance(health,dict) or not isinstance(health.get('seen'),dict):return False
        seen=health['seen'].get(self.client)
        if type(seen) not in (int,float) or not math.isfinite(seen):return False
        return (health.get('epoch')==self.epoch and health.get('generation')==self.fence
                and 0<=now-seen<3.)
    def expired(self,now,health):
        with self.lock:
            return self.until is not None and (now>=self.until or not self.valid_client(health,now))
    def status(self,now):
        with self.lock:return {'generation':self.generation,'active':self.active,'ready':self.until is not None and now<self.until,
                               'remaining_seconds':max(0.,self.until-now) if self.until is not None else 0.}


def health(root):
    try:
        value=json.loads((root/'data/standby-supervision.json').read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def configuration_stamp(runtime):
    """Watch the actual configuration/recipe/model files, never saved decisions."""
    root=runtime.shared_root();data=runtime.data_dir
    paths={data/'profile.json',data/'jigs.json',root/'integration/recipes.json',
           root/'integration/execution-settings.json',root/'data/workcell-preview.json',
           root/'data/jig-model-runtime.json'}
    profile=runtime.profile or {}
    if profile.get('calibration_file'):
        calibration=data/profile['calibration_file'];paths.update((calibration,calibration.with_suffix('.angles.json')))
    catalog=getattr(runtime,'catalog',None)
    if catalog:
        paths.update(Path(item['stl']) for item in catalog.items.values() if item.get('stl'))
    try:
        recipes=json.loads((root/'integration/recipes.json').read_text())['recipes'][runtime.robot_id]
        paths.update(data/'episodes'/(item['episode_id']+'.json') for item in recipes.values())
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    from .domain import ROOT
    paths.update(ROOT/'inspection'/name for name in ('roi_reference.json','shape_templates.json','shape_templates.npz','appearance_reference.json'))
    try:
        config=json.loads((root/'data/jig-model-runtime.json').read_text())
        for folder in config['models'].values():
            folder=Path(folder);paths.add(folder/'manifest.json')
            manifest=json.loads((folder/'manifest.json').read_text())
            paths.update(folder/name for name in manifest['files'])
        paths.update(Path(config['app_dir'])/'so101_teach'/name for name in ('yolo_worker.py','yolo_inference.py','inference_protocol.py'))
    except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
    def stamp(path):
        try:
            stat=path.stat();return stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns
        except OSError:return None
    return {str(path):stamp(path) for path in paths}


class InspectorCache:
    """Keep only model/geometry resources. Per-job evidence lives in the proxy."""
    def __init__(self):self.prepared=None;self.key=None;self.lock=threading.RLock()
    def prepare(self,profile,compute=None):
        from .episode_inspection_runtime import PreparedInspector
        from .model_regions import checked_settings
        from .ai_inspection import inspection_settings
        # Resolve the selected model on every use, including changes on disk.
        from .domain import ROOT
        assets={name:hashlib.sha256((ROOT/'inspection'/name).read_bytes()).hexdigest()
                for name in ('roi_reference.json','shape_templates.json','shape_templates.npz','appearance_reference.json')}
        key=json.dumps([profile,checked_settings(profile),inspection_settings(profile),assets,
                        getattr(compute,'descriptor',None)],sort_keys=True)
        with self.lock:
            if self.key!=key or self.prepared is None or self.prepared.closed.is_set():
                self.clear()
                self.prepared=PreparedInspector(profile,compute=compute);self.key=key
            return self.prepared
    def clear(self):
        with self.lock:
            old,self.prepared=self.prepared,None;self.key=None
            if old:
                old.closed.set()
                # Never hold the runtime control path behind native inference.
                threading.Thread(target=old.close,name='standby-inspector-close',daemon=True).start()


class RemotePreparedInspector:
    """A per-job view of the existing runtime's resident inspector."""
    def __init__(self,link,profile):
        self.link=link;self.compute=getattr(link,'compute',None);self.profile=deepcopy(profile);self.visual=None;self.evidence=None
        self.closed=threading.Event();self.ready=threading.Event();self.error=None
        link.rpc('inspection_prepare',{'profile':profile});self.ready.set()
    def borrow(self):
        owner=self
        class Borrowed:
            supports_evidence=True
            stop=None
            def inspect(self,frame,anchors,profile,*,inference=None,context_anchors=None,evidence=None):
                from .ai_inspection import inspection_settings
                from .decision_evidence import notify
                if inference is not None:inference.get(frame,inspection_settings(profile))
                wire=inference.to_wire() if inference is not None and inference.rows is not None else None
                value=owner.call(self.stop,'inspect',frame=frame,anchors=anchors,profile=profile,
                                 inference=wire,context_anchors=context_anchors)
                if value is None:return []
                notify(evidence,frame,value['detections'],value['rows']);return value['rows']
            def predict_all(self,frame,settings):return owner.call(self.stop,'predict_all',frame=frame,settings=settings) or []
            def prepare_geometry(self,anchors,profile,shape):return owner.call(self.stop,'prepare_geometry',anchors=anchors,profile=profile,shape=shape)
            def close(self):pass
        return Borrowed()
    def call(self,stop,method,**args):
        from .pc_compute import encode,decode
        if self.closed.is_set() or stop is not None and stop.is_set():return None
        value=self.link.rpc('inspection_call',{'profile':self.profile,'method':method,'payload':encode(args)})
        if self.closed.is_set() or stop is not None and stop.is_set():return None
        return decode(value)
    def close(self):
        self.closed.set()
        if self.visual:self.visual.close();self.visual=None
