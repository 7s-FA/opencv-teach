"""Incremental startup checks; all motion/readiness gates remain with the caller."""
from copy import deepcopy
import base64
import json
import time

from .domain import ROOT
from .episode_inspection import InspectionDecision
from .episode_inspection_runtime import FrameInspections


class StartupInspection:
    def __init__(self,steps,profile,catalog,placement,prepared_inspector,emit,*,linear_target=None):
        self.steps={s['id']:deepcopy(s) for s in steps}
        self.profile=deepcopy(profile);self.catalog=catalog;self.placement=deepcopy(placement)
        self.prepared_inspector=prepared_inspector;self.emit=emit;self.linear_target=linear_target
        reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
        self.dependencies={key:(None if s['inspection']['station']=='linear' else
            reference['stations']['carrier' if s['inspection']['station']=='carrier' else 'finished_pallet']['jig_id'])
            for key,s in self.steps.items()}
        self.signatures={};self.decisions={};self.passed={};self.counts={}
        self.worker=None;self.retired=[];self.submitted=None;self.observed=None
        self.linear_ready=False;self.context=None

    def _replace_worker(self,current,placement):
        if self.worker:
            self.worker.close();self.retired.append(self.worker)
        self.retired=[w for w in self.retired if w.is_alive()]
        active=[s for k,s in self.steps.items() if k in self.decisions and k not in self.passed]
        warm_placement=deepcopy(placement)
        if warm_placement and self.linear_target is not None:
            # Prediction of target geometry only, never evidence of arrival.
            warm_placement['linear_stage']['startup_state']={
                'known':True,'commanded_mm':self.linear_target,'moving':False}
        self.worker=FrameInspections(ROOT/'data',self.profile,self.catalog,placement,
            active or list(self.steps.values()),fixed_jigs=current,prepared_inspector=self.prepared_inspector,
            geometry_steps=list(self.steps.values()),geometry_placement=warm_placement)
        self.worker.retain([s['id'] for s in active])
        self.observed=None

    def poll(self,current,frame=None,*,linear_state=None,now=None):
        now=time.monotonic() if now is None else now
        placement=deepcopy(self.placement)
        has_linear=any(s['inspection']['station']=='linear' for s in self.steps.values())
        ready=False
        if has_linear:
            state=linear_state or {}
            ready=(state.get('phase')=='TIMED_COMPLETE'
                   and type(state.get('target_mm')) in (int,float)
                   and state['target_mm']==self.linear_target)
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
        self.signatures=signatures
        # Only relevant jig changes replace the worker. Completed unrelated
        # motion-target jigs do not invalidate an inspection.
        relevant={jid:deepcopy(current[jid]) for jid in set(self.dependencies.values()) if jid in current}
        context=(relevant,deepcopy(placement),deepcopy(signatures))
        if context!=self.context:
            self.context=context
            self._replace_worker(relevant,placement)
        active=set(self.decisions)-set(self.passed)
        if frame and active:
            if frame.get('error'):raise RuntimeError('안착 검사 카메라 오류: '+frame['error'])
            image=frame.get('preview_image') or frame.get('image')
            at=frame.get('preview_at') if frame.get('preview_image') else frame.get('at')
            if image and type(at) in (int,float) and 0<=now-at<1 and (self.submitted is None or at>self.submitted):
                import cv2
                import numpy as np
                decoded=cv2.imdecode(np.frombuffer(base64.b64decode(image,validate=True),np.uint8),cv2.IMREAD_COLOR)
                if decoded is None:raise RuntimeError('안착 검사 영상 해독 실패')
                self.worker.submit(decoded,at);self.submitted=at
        self.observed=self.worker.result
        if self.observed and self.observed.get('error'):
            raise RuntimeError('안착 검사 오류: '+self.observed['error'])
        rows=(self.observed or {}).get('results',{})
        for key in active:
            decision=self.decisions[key]
            try:passed=decision.observe(rows.get(key),now)
            except ValueError as exc:raise ValueError(self.steps[key]['name']+' · '+str(exc)) from exc
            if passed:
                step=deepcopy(self.steps[key])
                if decision.basis:step['inspection_basis']=deepcopy(decision.basis)
                self.passed[key]=step
                self.emit('INSPECTION_PROVISIONAL_PASS:'+key)
            if self.counts.get(key)!=decision.count:
                self.counts[key]=decision.count
                self.emit('INSPECTION_COUNT:'+key+f":{decision.count}/{decision.check.get('minimum_observations',2)}")
        self.worker.retain(set(self.decisions)-set(self.passed))

    @property
    def complete(self):
        return self.passed.keys()==self.steps.keys()

    def close(self):
        workers=self.retired+([self.worker] if self.worker else [])
        for worker in workers:worker.close()
        for worker in workers:worker.join()
        self.retired=[];self.worker=None
