"""Incremental startup checks; all motion/readiness gates remain with the caller."""
from copy import deepcopy
import base64
import json
import math
import time

from .domain import ROOT
from .episode_inspection import InspectionDecision
from .episode_faults import InspectionRetry
from .episode_inspection_runtime import FrameInspections
from .preparation_profile import timed


class StartupInspection:
    def __init__(self,steps,profile,catalog,placement,prepared_inspector,emit,*,linear_target=None,timing=None,timing_prefix='inspection'):
        self.timing=timing;self.timing_prefix=timing_prefix
        if timing:timing.mark(timing_prefix+'.started')
        self.steps={s['id']:deepcopy(s) for s in steps}
        self.profile=deepcopy(profile);self.catalog=catalog;self.placement=deepcopy(placement)
        self.prepared_inspector=prepared_inspector;self.emit=emit;self.linear_target=linear_target
        reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
        self.dependencies={key:(None if s['inspection']['station']=='linear' else
            reference['stations']['carrier' if s['inspection']['station']=='carrier' else 'finished_pallet']['jig_id'])
            for key,s in self.steps.items()}
        self.signatures={};self.decisions={};self.passed={};self.counts={}
        self.worker=None;self.submitted=None;self.inflight=None;self.observed=None
        self.linear_ready=False;self.context=None

    def _update_worker(self,current,placement):
        active=[s for k,s in self.steps.items() if k in self.decisions and k not in self.passed]
        if self.worker:
            # Keep the model/geometry cache, but never publish a result computed
            # against an earlier association context, including neighbouring jigs.
            self.worker.update_context(current,placement,[s['id'] for s in active])
        elif active:
            self.worker=timed(self.timing,self.timing_prefix+'.worker_create',FrameInspections,ROOT/'data',self.profile,self.catalog,placement,
                list(self.steps.values()),fixed_jigs=current,prepared_inspector=self.prepared_inspector,
                timing=self.timing,timing_prefix=self.timing_prefix)
            self.worker.retain([s['id'] for s in active])
        # Geometry is projected/cache-filled only for actual inspection anchors.
        # No speculative worker or projection is needed before a target is ready.
        self.observed=None;self.inflight=None

    def poll(self,current,frame=None,*,linear_state=None,now=None):
        now=time.monotonic() if now is None else now
        placement=deepcopy(self.placement)
        has_linear=any(s['inspection']['station']=='linear' for s in self.steps.values())
        state=linear_state or {};target=state.get('target_mm')
        known=(state.get('phase')=='TIMED_COMPLETE' and type(target) in (int,float) and math.isfinite(target))
        ready=known and target==self.linear_target
        if placement and 'linear_stage' in placement:
            # Association geometry must come from current controller evidence,
            # including when only carrier/finished checks are requested.
            placement['linear_stage']['startup_state']={
                'known':known,'commanded_mm':target if known else None,
                'moving':state.get('phase')=='MOVING','pending':not known}
        if has_linear:
            if self.linear_ready and not ready:
                raise RuntimeError('시작 전 검사 중 리니어 상태 변경')
            self.linear_ready=ready
            if ready:
                if not placement:raise RuntimeError('시작 전 검사: 작업대 좌표 없음')
                placement['linear_stage']['startup_state']={
                    'known':True,'commanded_mm':state['target_mm'],'moving':False}
        signatures={}
        for key,step in self.steps.items():
            dependency=self.dependencies[key]
            if dependency is None:
                if ready:signatures[key]={'linear_target':self.linear_target}
            elif dependency in current:signatures[key]=deepcopy(current[dependency])
        for key in set(self.signatures)|set(signatures):
            if self.signatures.get(key)!=signatures.get(key):
                self.decisions.pop(key,None);self.passed.pop(key,None);self.counts.pop(key,None)
                if key in self.signatures:self.emit('INSPECTION_INVALIDATED:'+key)
                if key in signatures:
                    step=self.steps[key];check=step['inspection']
                    self.decisions[key]=InspectionDecision(check,step['inspection_product'],now,
                        frame_gap=check['timeout_seconds'],ignore_missing=True)
                    self.emit('INSPECTION_STARTED:'+key+':'+step['name'])
                    if self.timing:self.timing.mark(self.timing_prefix+'.dependency_ready.'+key)
        self.signatures=signatures
        # Every adopted jig supplies association geometry. Only each target's
        # own dependency above invalidates its count or provisional pass.
        surrounding=deepcopy(current)
        context=(surrounding,deepcopy(placement),deepcopy(signatures))
        if context!=self.context:
            self.context=context
            self._update_worker(surrounding,placement)
        active=set(self.decisions)-set(self.passed)
        if not self.worker:return
        if frame and active and frame.get('error'):
            raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
        # Consume before submitting: the last required observation must prevent
        # another inference, rather than racing a frame already in the queue.
        self.observed=self.worker.result
        if self.observed and self.observed.get('error'):
            raise RuntimeError('안착 검사 오류: '+self.observed['error'])
        if self.observed and self.observed.get('at')==self.inflight:self.inflight=None
        rows=(self.observed or {}).get('results',{})
        for key in active:
            decision=self.decisions[key]
            try:passed=decision.observe(rows.get(key),now)
            except ValueError as exc:
                kind=InspectionRetry if isinstance(exc,InspectionRetry) else ValueError
                raise kind(self.steps[key]['name']+' · '+str(exc)) from exc
            if passed:
                step=deepcopy(self.steps[key])
                if decision.basis:step['inspection_basis']=deepcopy(decision.basis)
                self.passed[key]=step
                self.emit('INSPECTION_PROVISIONAL_PASS:'+key)
                if self.timing:self.timing.mark(self.timing_prefix+'.passed.'+key)
            if self.counts.get(key)!=decision.count:
                self.counts[key]=decision.count
                self.emit('INSPECTION_COUNT:'+key+f":{decision.count}/{decision.check.get('minimum_observations',2)}")
                if self.timing:self.timing.mark(self.timing_prefix+'.observation_'+str(decision.count)+'.'+key)
        active=set(self.decisions)-set(self.passed)
        self.worker.retain(active)
        if frame and active and self.inflight is None and self.worker.accepting_frame:
            image=frame.get('preview_image') or frame.get('image')
            at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
            if (image and type(at) in (int,float) and 0<=now-at<1
                    and (self.submitted is None or at>self.submitted)
                    and any(self.decisions[key].started<=at for key in active)):
                import cv2
                import numpy as np
                decoded=timed(self.timing,self.timing_prefix+'.frame_decode',lambda:cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR))
                if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                if self.worker.submit(decoded,at) is not False:
                    self.submitted=at;self.inflight=at

    @property
    def complete(self):
        return self.passed.keys()==self.steps.keys()

    def close(self):
        if self.worker:
            self.worker.close();self.worker.join();self.worker=None
