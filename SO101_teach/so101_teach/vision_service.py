from copy import deepcopy
import threading,time
from .vision import PoseLatch,detect
class MultiDetector:
    def __init__(self,catalog,profile,active='pallet',default_latch=None):
        self.catalog=catalog;self.profile=profile;self.active=active;self.lock=threading.RLock();self.meshes={};self.revision=-1
        self.latches={active:default_latch or PoseLatch()};self.frozen=False;self.seconds=10.;self.generation=0;self.sequence=0
        self.acquisition_seconds=self.latches[active].stability.seconds;self.attempts_limit=self.latches[active].stability.attempts_limit
    def refresh(self):
        if self.revision!=self.catalog.revision:
            self.meshes={k:self.catalog.mesh(k) for k in self.catalog.items};self.revision=self.catalog.revision
            for key in self.meshes:self.latches.setdefault(key,PoseLatch(self.seconds,self.acquisition_seconds,self.attempts_limit))
    def clear(self,key=None):
        with self.lock:
            if self.frozen:return False
            self.generation+=1
            for k,v in self.latches.items():
                if key is None or key==k:v.clear(time.monotonic())
            return True
    def freeze(self,enabled):
        with self.lock:
            self.frozen=bool(enabled)
            for latch in self.latches.values():latch.freeze(enabled)
    def process(self,frame):
        began=time.monotonic()
        with self.lock:
            self.refresh();items=deepcopy(self.catalog.items);meshes=deepcopy(self.meshes);revision=self.catalog.revision;generation=self.generation;base_profile=deepcopy(self.profile)
        from .lens_geometry import DetectionFrame
        from .height_reference import support_plane_profile
        prepared=DetectionFrame(frame,base_profile);live_results={};profiles={}
        for key,config in items.items():
            profiles[key]=support_plane_profile(base_profile,config)
            raw=detect(frame,meshes[key],config.get('roi'),profiles[key],prepared=prepared)
            live_results[key]={**raw,'pose_held':False,'pose_frozen':False,'hold_remaining_s':0.}
        with self.lock:
            if revision!=self.catalog.revision or generation!=self.generation:return {'selected':None,'candidates':[],'status':'settings_changed','by_jig':{}}
            # Commit a whole frame together. A polling reader must never combine
            # one jig from the in-flight frame with another from the last image.
            now=time.monotonic();self.sequence+=1
            results={key:self.latches[key].update(raw,now,profile=profiles[key],mesh=meshes[key]) for key,raw in live_results.items()}
            main=deepcopy(results.get(self.active,{'selected':None,'candidates':[],'status':'not_found'}))
            main.update(by_jig=results,live_by_jig=live_results,detector_generation=generation,detector_sequence=self.sequence,processing_seconds=time.monotonic()-began)
            return main

    def finish_observation(self,detection,now):
        if not detection or not detection.get('by_jig'):return detection
        with self.lock:
            if detection.get('detector_generation')!=self.generation or detection.get('detector_sequence')!=self.sequence:return detection
            results={key:(self.latches[key].poll(now) or value) if key in self.latches else value
                     for key,value in detection['by_jig'].items()}
            main=deepcopy(results.get(self.active,detection));main.update(by_jig=results,live_by_jig=detection.get('live_by_jig',{}),detector_generation=self.generation,detector_sequence=self.sequence,processing_seconds=detection.get('processing_seconds'))
            return main
