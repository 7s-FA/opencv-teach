from copy import deepcopy
import threading,time
import hashlib
from .vision import PoseLatch,detect
from .mask_pose import detect_mask_seeded
from .two_frame_gate import carrier_observation_reason
class MultiDetector:
    def __init__(self,catalog,profile,active='pallet',default_latch=None):
        self.catalog=catalog;self.profile=profile;self.active=active;self.lock=threading.RLock();self.meshes={};self.revision=-1
        self.latches={active:default_latch or PoseLatch()};self.frozen=False;self.seconds=10.;self.generation=0;self.sequence=0
        self.acquisition_seconds=self.latches[active].stability.seconds;self.attempts_limit=self.latches[active].stability.attempts_limit
        self.region_model=None;self.region_tracks={};self.region_retry_after={};self.region_context=None
    def close(self):
        if self.region_model:self.region_model.close()
        with self.lock:
            self.region_tracks.clear();self.region_retry_after.clear();self.region_context=None;self.generation+=1
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
            if key is None:self.region_tracks.clear();self.region_retry_after.clear()
            else:self.region_tracks.pop(key,None);self.region_retry_after.pop(key,None)
            return True
    def freeze(self,enabled):
        with self.lock:
            self.frozen=bool(enabled)
            for latch in self.latches.values():latch.freeze(enabled)
    def prepare(self,frame):
        """Prepare inference without filling latches, tracks or saved poses."""
        from .model_regions import checked_settings,RegionModel
        began=time.monotonic();settings=checked_settings(self.profile);called=False
        if settings['method']=='model_roi':
            if self.region_model is None:self.region_model=RegionModel()
            called=self.region_model.prepare(frame,settings)
        return {'status':'prepared','vision_ready':True,'selected':None,'candidates':[],
                'model_inference_called':called,'processing_seconds':time.monotonic()-began}
    def process(self,frame):
        began=time.monotonic()
        with self.lock:
            self.refresh();items=deepcopy(self.catalog.items);meshes=deepcopy(self.meshes);revision=self.catalog.revision;generation=self.generation;base_profile=deepcopy(self.profile)
            context=(frame.shape,revision,base_profile,items)
            if context!=self.region_context:
                previous=self.region_context
                self.region_tracks.clear();self.region_retry_after.clear();self.region_context=deepcopy(context)
                model_changed=previous is not None and any(p.get('jig_detection',{}).get('method')=='model_roi' for p in (base_profile,previous[2]))
                if model_changed and not self.frozen:
                    for latch in self.latches.values():latch.clear(began)
            tracks=deepcopy(self.region_tracks);retry_after=dict(self.region_retry_after)
            frozen=self.frozen
        from .lens_geometry import DetectionFrame
        from .height_reference import support_plane_profile
        prepared=DetectionFrame(frame,base_profile);live_results={};profiles={}
        from .model_regions import checked_settings,RegionModel,tracking_regions,regions_mask,expanded_tracking_regions
        settings=checked_settings(base_profile);detections=[];model_error=None;model_called=False;model_seconds=0.;opencv_seconds=0.;lost=set();pending=[]
        def rim(key,mask=None):
            nonlocal opencv_seconds
            start=time.monotonic()
            options={'search_mask':mask} if mask is not None else {}
            result=detect(frame,meshes[key],items[key].get('roi'),profiles[key],prepared=prepared,**options)
            opencv_seconds+=time.monotonic()-start
            return result
        if settings['method']!='model_roi' and self.region_model:self.region_model.close()
        for key,config in items.items():
            profiles[key]=support_plane_profile(base_profile,config)
            if settings['method']=='model_roi':
                regions=tracks.get(key,[])
                # A CAD silhouette pose needs this frame's mask. Reusing a
                # previous mask would freeze a moved pallet's position. Keep
                # the carrier's existing local OpenCV tracking unchanged.
                if regions and meshes[key].get('shape')!='ring':
                    raw=rim(key,regions_mask(frame,regions));next_regions=tracking_regions(frame,raw)
                    if not next_regions:
                        # Grid fits can lose supporting fragments at a search-window
                        # edge. Try 16 extra pixels locally before calling the model.
                        regions=expanded_tracking_regions(frame,regions)
                        raw=rim(key,regions_mask(frame,regions));next_regions=tracking_regions(frame,raw)
                        raw['tracking_local_retry']=True
                    if next_regions:
                        tracks[key]=next_regions;raw.update(model_regions=regions,tracking_state='tracking');live_results[key]=raw;continue
                    tracks.pop(key,None);retry_after.pop(key,None);lost.add(key)
                pending.append(key)
            else:live_results[key]=rim(key)
        # All lost/uninitialized jigs share one inference on this exact frame.
        # Absent objects retry at most once per second after an unsuccessful search.
        if pending and any(began>=retry_after.get(key,0.) for key in pending):
            model_called=True;start=time.monotonic()
            try:
                if self.region_model is None:self.region_model=RegionModel()
                detections=self.region_model.predict(frame,settings)
            except Exception as exc:model_error='모델 영역 검출 실패 · '+str(exc)
            model_seconds=time.monotonic()-start
        for key in pending:
            regions=[]
            if model_called and not model_error:
                start=time.monotonic()
                try:
                    raw=detect_mask_seeded(frame,meshes[key],items[key].get('roi'),profiles[key],detections,settings,prepared=prepared)
                except (ValueError,KeyError) as exc:
                    raw={'selected':None,'candidates':[],'status':'model_error','error':'마스크 후보 보정 실패 · '+str(exc),'verified_for_motion':False}
                opencv_seconds+=time.monotonic()-start;regions=raw.get('model_regions',[])
            else:
                raw={'selected':None,'candidates':[],'status':'model_error' if model_error else 'model_not_found',
                     'error':model_error or ('모델에서 해당 지그 영역을 찾지 못했습니다.' if model_called else '지그 재탐색 대기 중'),
                     'verified_for_motion':False}
            next_regions=tracking_regions(frame,raw)
            if next_regions:tracks[key]=next_regions;retry_after.pop(key,None)
            elif model_called:retry_after[key]=time.monotonic()+1.
            raw.update(model_regions=regions,tracking_state=('reacquired' if key in lost else 'acquired') if next_regions else 'lost')
            live_results[key]=raw
        for raw in live_results.values():
            if settings['method']=='model_roi':raw.update(detection_method='model_roi',region_model=settings['model'],model_identity=settings['model_identity'])
            raw.update(pose_held=False,pose_frozen=False,hold_remaining_s=0.)
        if settings['method']=='model_roi' and not frozen:
            digest=None
            for key,raw in live_results.items():
                # A vote must come from AI on this image, not a held pose or
                # a repeated camera image. Share the digest with the carrier observations.
                if (model_called and not model_error and meshes[key].get('shape')=='ring'
                        and raw.get('pallet_pose_method')=='cad_silhouette_fit'
                        and (raw.get('selected') or {}).get('cad_pose_validated')):
                    if digest is None:digest=hashlib.blake2s(frame.tobytes(),digest_size=16).digest()
                    raw['cad_observation_id']=digest.hex()
                if meshes[key].get('shape')=='rectangle':
                    reason=carrier_observation_reason(raw,meshes[key],frame.shape)
                    raw['carrier_three_frame_reason']=reason or 'eligible'
                    if reason is None:
                        if digest is None:digest=hashlib.blake2s(frame.tobytes(),digest_size=16).digest()
                        raw['carrier_observation_id']=digest.hex()
        with self.lock:
            if revision!=self.catalog.revision or generation!=self.generation:return {'selected':None,'candidates':[],'status':'settings_changed','by_jig':{}}
            self.region_tracks=tracks;self.region_retry_after=retry_after
            # Commit a whole frame together. A polling reader must never combine
            # one jig from the in-flight frame with another from the last image.
            now=time.monotonic();self.sequence+=1
            if settings['method']=='model_roi' and not self.frozen:
                for key,raw in live_results.items():
                    if key in lost or not raw.get('selected'):self.latches[key].clear(now)
            results={key:self.latches[key].update(raw,now,profile=profiles[key],mesh=meshes[key]) for key,raw in live_results.items()}
            main=deepcopy(results.get(self.active,{'selected':None,'candidates':[],'status':'not_found'}))
            main.update(by_jig=results,live_by_jig=live_results,detector_generation=generation,detector_sequence=self.sequence,processing_seconds=time.monotonic()-began,
                        model_inference_called=model_called,model_seconds=model_seconds,opencv_seconds=opencv_seconds)
            return main

    def finish_observation(self,detection,now):
        if not detection or not detection.get('by_jig'):return detection
        with self.lock:
            if detection.get('detector_generation')!=self.generation or detection.get('detector_sequence')!=self.sequence:return detection
            results={key:(self.latches[key].poll(now) or value) if key in self.latches else value
                     for key,value in detection['by_jig'].items()}
            main=deepcopy(results.get(self.active,detection));main.update(by_jig=results,live_by_jig=detection.get('live_by_jig',{}),detector_generation=self.generation,detector_sequence=self.sequence,processing_seconds=detection.get('processing_seconds'))
            for name in ('model_inference_called','model_seconds','opencv_seconds'):main[name]=detection.get(name)
            return main
