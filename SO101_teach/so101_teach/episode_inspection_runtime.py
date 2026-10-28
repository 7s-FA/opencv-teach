"""Fresh-frame inspection worker shared by PC and Pi checkpoint execution."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from .domain import ROOT
from .episode_faults import InspectionRetry
from .episode_inspection import STATIONS,target_anchor
from .preparation_profile import timed


class PreparedInspector:
    """One execution owns the model; observations and cancellation stay per worker."""
    def __init__(self,profile,*,timing=None,timing_prefix='inspection',compute=None):
        self.compute=compute
        require_resources()
        self.profile=deepcopy(profile)
        self.timing=timing;self.timing_prefix=timing_prefix
        self.lock=threading.RLock();self.ready=threading.Event();self.closed=threading.Event()
        self.inspector=None;self.error=None;self.visual=None;self.evidence=None
        self.thread=threading.Thread(target=self._prepare,daemon=False,name='inspection-model-prepare')
        self.thread.start()
    def _prepare(self):
        try:
            from .ai_inspection import inspector_for,inspection_settings
            import numpy as np
            options={'compute':self.compute} if self.compute is not None else {}
            self.inspector=timed(self.timing,self.timing_prefix+'.model_create',inspector_for,self.profile,**options)
            if hasattr(self.inspector,'timing'):
                self.inspector.timing=self.timing;self.inspector.timing_prefix=self.timing_prefix
            if hasattr(self.inspector,'provider') and not self.closed.is_set():
                # Synthetic warm-up is discarded. It cannot enter any decision,
                # jig latch, frame queue, geometry cache or inspection result.
                timed(self.timing,self.timing_prefix+'.model_warmup',self.inspector.provider.prepare,np.zeros((480,640,3),np.uint8),inspection_settings(self.profile))
        except Exception as exc:self.error=exc
        finally:self.ready.set()
    def borrow(self):
        owner=self
        class BorrowedInspector:
            supports_evidence=True
            stop=None
            def inspect(self,frame,anchors,profile,*,inference=None,context_anchors=None,evidence=None):
                return owner.inspect(frame,anchors,profile,self.stop,inference=inference,context_anchors=context_anchors,evidence=evidence)
            def predict_all(self,frame,settings):
                return owner._call(owner.profile,self.stop,'predict_all',frame,settings)
            def prepare_geometry(self,anchors,profile,shape):
                return owner._call(profile,self.stop,'prepare_geometry',anchors,profile,shape)
            def close(self):pass
        return BorrowedInspector()
    def inspect(self,frame,anchors,profile,stop,*,inference=None,context_anchors=None,evidence=None):
        # Acquire frame evidence before the inspector lock. Its predictor may
        # borrow this same prepared model from another consumer/thread.
        if inference is not None and anchors and not self.closed.is_set() and not (stop is not None and stop.is_set()):
            from .ai_inspection import inspection_settings
            inference.get(frame,inspection_settings(profile))
        options={'inference':inference} if inference is not None else {}
        if context_anchors is not None:options['context_anchors']=context_anchors
        if evidence is not None:options['evidence']=evidence
        return self._call(profile,stop,'inspect',frame,anchors,profile,**options)
    def _call(self,profile,stop,method,*args,**kwargs):
        def cancelled():return self.closed.is_set() or stop is not None and stop.is_set()
        began=time.monotonic()
        while not self.ready.wait(.05):
            if cancelled():return []
        if cancelled():return []
        if self.error is not None:raise RuntimeError('안착 검사 모델 준비 실패: '+str(self.error)) from self.error
        if profile!=self.profile:raise ValueError('준비된 안착 검사 프로필과 현재 프로필이 다릅니다.')
        if self.timing:self.timing.add(self.timing_prefix+'.model_ready_wait',time.monotonic()-began)
        began=time.monotonic()
        while not self.lock.acquire(timeout=.05):
            if cancelled():return []
        try:
            if self.timing:self.timing.add(self.timing_prefix+'.model_lock_wait',time.monotonic()-began)
            if cancelled():return []
            self.inspector.stop=stop
            if method=='prepare_geometry' and not hasattr(self.inspector,method):return
            callback=None
            if method=='inspect' and not getattr(self.inspector,'supports_evidence',False):callback=kwargs.pop('evidence',None)
            result=timed(self.timing,self.timing_prefix+'.'+method+'_call',getattr(self.inspector,method),*args,**kwargs)
            if callback is not None:
                from .decision_evidence import notify
                notify(callback,args[0],[],result)
            return result
        finally:self.lock.release()
    def close(self):
        self.closed.set();self.thread.join()
        with self.lock:
            if self.inspector is not None:
                if hasattr(self.inspector,'close'):self.inspector.close()
                self.inspector=None
        if self.visual:self.visual.close();self.visual=None


class InspectionBatch:
    """Capture while held at a checkpoint; classify only that immutable frame set."""
    CAPTURE_SECONDS=2.

    def __init__(self,product,make_worker,on_pass,*,evidence=None):
        from .assembly_evidence import AssemblyEvidence
        self.evidence=evidence if evidence is not None else AssemblyEvidence()
        self.product=product;self.make_worker=make_worker;self.on_pass=on_pass;self.pending=[]

    @property
    def capturing(self):return any(item['frozen_at'] is None for item in self.pending)

    def start(self,step):
        from .episode_inspection import InspectionDecision
        now=time.monotonic();worker=self.make_worker(step)
        # Batch membership proves capture freshness. Processing has its own
        # finite deadline; wall age must not invalidate a queued snapshot.
        decision=InspectionDecision(step['inspection'],self.product,now,
            frame_gap=self.CAPTURE_SECONDS,evidence=self.evidence,
            max_frame_age=None,deadline=float('inf'))
        self.pending.append({'step':deepcopy(step),'worker':worker,'decision':decision,
            'frames':[],'frozen_at':None,'deadline':None,'next':0,'inflight':None})

    def _poll_item(self,item,view,now,observation):
        decision=item['decision'];worker=item['worker']
        if observation and observation.get('error'):
            raise ValueError('안착 검사 오류: '+observation['error'])
        if item['frozen_at'] is None:
            if now-decision.started>=self.CAPTURE_SECONDS:
                raise InspectionRetry('검사 시점 영상 확보 시간 초과: '+str(len(item['frames']))+'장 수신')
            if (view and decision.started<view[1]<=now and now-view[1]<1
                    and (not item['frames'] or view[1]>item['frames'][-1][1])):
                item['frames'].append((view[0].copy(),view[1]))
            if len(item['frames'])<decision.check.get('minimum_observations',2):return False,observation
            item['frozen_at']=now
            item['deadline']=now+decision.check['timeout_seconds']
        # One submitted image at a time prevents the worker's result slot from
        # skipping evidence. Results from another capture are never consumed.
        if item['inflight'] is not None and observation and observation.get('at')==item['inflight']:
            completed=observation.get('completed',now)
            if type(completed) not in (int,float) or not item['frozen_at']<=completed<=now or completed>item['deadline']:
                raise InspectionRetry('검사 영상 처리 시간 초과')
            item['inflight']=None;item['next']+=1
            if decision.observe(observation,now):return True,observation
            if item['next']==len(item['frames']):
                raise InspectionRetry('검사 시점 영상 판정 불가: '+decision.reason)
        if now>=item['deadline']:raise InspectionRetry('검사 영상 처리 시간 초과')
        if item['inflight'] is None:
            frame,at=item['frames'][item['next']]
            if worker.submit(frame,at) is not False:item['inflight']=at
        return False,observation

    def poll(self,view=None,*,frozen_only=False):
        for item in list(self.pending):
            # Stop handling may drain existing evidence, but must never take a
            # replacement image or expire a partially captured pose mid-pause.
            if frozen_only and item['frozen_at'] is None:continue
            decision=item['decision']
            # Read once so failure evidence cannot race a newer worker result.
            observation=item['worker'].result
            now=time.monotonic()
            try:
                passed,observation=self._poll_item(item,view,now,observation)
            except ValueError as exc:
                kind=InspectionRetry if isinstance(exc,InspectionRetry) else ValueError
                failure=kind(item['step']['name']+' · '+str(exc))
                failure.inspection_snapshot={'steps':[deepcopy(item['step'])],'result':deepcopy(observation),
                    'result_role':'failure_decision_input','decision':{'started':decision.started,
                        'checked_at':now,'captured_at':[at for _,at in item['frames']],
                        'frozen_at':item['frozen_at'],'processing_deadline':item['deadline'],
                        'count':decision.count,'last_counted_at':decision.last_counted_at,'last_good':decision.last_good}}
                raise failure from exc
            if passed:
                step=item['step']
                step['inspection_capture']={'started':decision.started,'captured_at':[at for _,at in item['frames']],
                    'frozen_at':item['frozen_at'],'processed_at':now}
                if decision.basis:step['inspection_basis']=deepcopy(decision.basis)
                self.evidence.record(step,decision.product)
                self.on_pass(step);item['worker'].close();self.pending.remove(item)

    def recapture(self):
        # The caller must first validate that the arm remains at the checkpoint.
        # Discard all old images/results and create fresh observation counters.
        steps=[deepcopy(item['step']) for item in self.pending]
        self.close()
        for step in steps:self.start(step)

    def restart(self):
        # A pause during later motion cannot turn new-position images into
        # evidence for a previous checkpoint. Frozen processing continues.
        if self.capturing:raise ValueError('검사 영상 확보 중 중단됨: 해당 검사 위치에서 다시 실행하세요.')

    def close(self):
        pending,self.pending=self.pending,[]
        for item in pending:item['worker'].close()


def require_resources():
    for name in ('roi_reference.json','shape_templates.json','shape_templates.npz','appearance_reference.json'):
        if not (ROOT/'inspection'/name).is_file():raise ValueError('안착 검사 기준 파일 없음: '+name)


def detect_inspection_frame(detector,frame,profile,inspector):
    if detector is None:return {},None
    from .shared_inference import shared_frame
    inference=shared_frame(frame,profile,inspector)
    detected=detector.process(frame,inference=inference) if inference is not None else detector.process(frame)
    return detected['live_by_jig'],inference


def read_initial_linear_state(data_dir):
    """Query the Pi controller's completed target; never claim or move it."""
    import shlex
    import subprocess
    from .pi_connection import load,validate
    from .remote_client import ssh_args
    config=validate(load(data_dir),True)
    script="import json,sys;from pathlib import Path;sys.path.insert(0,str(Path(sys.argv[1]).expanduser()/'integration'));from linear_client import LinearClient;print(json.dumps(LinearClient().call('status')))"
    command=' '.join(shlex.quote(v) for v in (config['python'],'-c',script,config['app_dir']))
    result=subprocess.run(ssh_args(config)+['-l',config['user'],config['host'],command],capture_output=True,text=True,timeout=8)
    if result.returncode:raise ValueError('시작 전 검사: Pi 리니어 상태 조회 실패')
    value=json.loads(result.stdout)
    if value.get('phase')!='TIMED_COMPLETE' or type(value.get('target_mm')) not in (int,float):raise ValueError('시작 전 검사: 리니어 정지·명령 위치 확인 필요')
    return {'known':True,'commanded_mm':value['target_mm'],'moving':False,'pending':False}


class FrameInspection(threading.Thread):
    def __init__(self,data_dir,profile,catalog,placement,check,product,*,linear_state_reader=None,fixed_jigs=None,prepared_inspector=None,visual_step=None,preserve_pending_frames=False):
        require_resources()
        # OpenCV must finish its current call before Python tears down native state.
        # Cancellation still returns promptly after signalling stop.
        super().__init__(daemon=False,name='episode-seating-check')
        self.data_dir=Path(data_dir);self.profile=deepcopy(profile);self.placement=deepcopy(placement);self.check=deepcopy(check);self.product=product
        self.linear_state_reader=linear_state_reader;self.fixed_jigs=deepcopy(fixed_jigs)
        self.prepared_inspector=prepared_inspector
        self.visual_step=deepcopy(visual_step)
        self.preserve_pending_frames=preserve_pending_frames
        meshes={key:deepcopy(catalog.mesh(key)) for key in catalog.items} if fixed_jigs is None else {}
        self.catalog=SimpleNamespace(items=deepcopy(catalog.items),revision=0,mesh=lambda key:meshes[key])
        self.stop=threading.Event();self.jobs=queue.Queue(1);self.result=None;self.start()
    def submit(self,frame,at):
        if self.stop.is_set():return False
        if self.preserve_pending_frames:
            # Preserve the first fresh post-checkpoint image waiting behind the
            # current inference. Later arm/part motion must not replace it.
            if self.jobs.full():return False
        else:
            try:self.jobs.get_nowait()
            except queue.Empty:pass
        copied=timed(getattr(self,'timing',None),getattr(self,'timing_prefix','inspection')+'.frame_copy',frame.copy)
        try:self.jobs.put_nowait((copied,at,time.monotonic()))
        except queue.Full:return False
        return True
    def close(self):
        self.stop.set()
        # Wake an idle worker immediately; an in-flight native call still owns
        # its frame/model until it returns and is joined normally.
        try:self.jobs.put_nowait(None)
        except queue.Full:pass
        if self.is_alive() and threading.current_thread() is not self:self.join(.2)
    def evidence_frame(self,frame,at,*,masks=(),metadata=None):
        recorder=getattr(self.prepared_inspector,'evidence',None)
        if recorder is None:return None
        return recorder.submit(frame,at,masks=masks,metadata={
            'kind':'inspection_processing','input_kind':'exact_inspection_input_after_transport_decode',
            'profile':self.profile,**(metadata or {})})
    def classify(self,inspector,frame,at,anchors,*,inference=None,context_anchors=None,metadata=None):
        self.frame_evidence_ref=None
        def record(image,masks,rows):
            self.frame_evidence_ref=self.evidence_frame(image,at,masks=masks,metadata={
                **(metadata or {}),'anchors':anchors,'context_anchors':context_anchors,'rows':rows,
                'mask_state':'current_inference' if masks else 'no_instances_or_no_inference'})
        enabled=getattr(self.prepared_inspector,'evidence',None) is not None
        options={'inference':inference} if inference is not None else {}
        if context_anchors is not None:options['context_anchors']=context_anchors
        if enabled and getattr(inspector,'supports_evidence',False):options['evidence']=record
        try:rows=inspector.inspect(frame,anchors,self.profile,**options)
        except Exception as exc:
            if enabled:self.frame_evidence_ref=self.evidence_frame(frame,at,metadata={**(metadata or {}),'error':str(exc),'mask_state':'unavailable_after_error'})
            raise
        if enabled and 'evidence' not in options:record(frame,[],rows)
        return rows
    def evidence_result(self,reference,result,step_id=None):
        if reference is not None:result['decision_evidence']={**reference,'step_id':step_id}
        return result
    def publish_visual(self,frame,at,observations):
        visual=getattr(self.prepared_inspector,'visual',None)
        if visual is None:return
        try:
            steps=getattr(self,'steps',None) or ([self.visual_step] if self.visual_step else [])
            visual.submit(frame,at,[{'step_id':s['id'],'step_name':s.get('name',''),
                'check':s['inspection'],'product':s.get('inspection_product',self.product),
                **observations[s['id']]} for s in steps if s['id'] in observations])
        except Exception as exc:visual.error=str(exc)  # Display cannot affect the decision.
    def run(self):
        inspector=None;detector=None
        try:
            from .ai_inspection import inspector_for
            from .vision_service import MultiDetector
            from .inspection_geometry import inspection_context
            from .occupied_pallet import locate
            inspector=self.prepared_inspector.borrow() if self.prepared_inspector is not None else inspector_for(self.profile)
            inspector.stop=self.stop
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            options={'compute':self.prepared_inspector.compute} if getattr(self.prepared_inspector,'compute',None) is not None else {}
            detector=MultiDetector(self.catalog,self.profile,**options) if self.fixed_jigs is None else None
            from .episode_inspection import fixed_jig_results
            fixed=fixed_jig_results(self.fixed_jigs) if self.fixed_jigs is not None else {}
            reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
            while not self.stop.is_set():
                try:job=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                if job is None or self.stop.is_set():break
                frame,at,queued=job
                fresh,inference=detect_inspection_frame(detector,frame,self.profile,inspector)
                if self.fixed_jigs is None and self.check['station']=='finished':
                    jid=reference['stations']['finished_pallet']['jig_id'];config=self.catalog.items.get(jid)
                    # Preserve the standard empty-pallet pose. Occupied recovery
                    # supplies inspection anchors only, never robot target poses.
                    observed=locate(frame,config,self.profile) if config and not fresh.get(jid,{}).get('selected') else None
                    if observed is not None:fresh[jid]=observed
                if self.check['station']=='linear':
                    state=(self.placement or {}).get('linear_stage',{}).get('startup_state',{})
                    if not state.get('known') or state.get('moving') or state.get('pending'):
                        self.result={'at':at,'row':None,'reason':'리니어 정지·명령 위치 확인 필요'}
                        self.evidence_result(self.evidence_frame(frame,at,metadata={'result':self.result,'check':self.check,'mask_state':'not_requested'}),self.result);continue
                # Compare both product families so selecting A cannot turn B into A.
                located,surrounding=inspection_context(frame,fresh,fixed,self.catalog.items,self.profile,reference,self.placement)
                anchors,reason=located[self.check['station']]
                chosen=[a for a in anchors if target_anchor(a,self.check,self.product)]
                if len(chosen)!=1 or not chosen[0]['inspection_allowed']:
                    self.result={'at':at,'row':None,'reason':('실행 전 확정 지그 위치 없음 · '+reason) if self.fixed_jigs is not None else reason}
                    self.evidence_result(self.evidence_frame(frame,at,metadata={'result':self.result,'check':self.check,'mask_state':'not_requested'}),self.result);continue
                rows=self.classify(inspector,frame,at,chosen,context_anchors=surrounding,inference=inference,
                    metadata={'check':self.check,'product':self.product,'fixed_jigs':self.fixed_jigs,'placement':self.placement})
                self.result={'at':at,'row':deepcopy(rows[0]) if len(rows)==1 else None,'reason':reason,'completed':time.monotonic()}
                self.evidence_result(self.frame_evidence_ref,self.result,(self.visual_step or {}).get('id'))
                if self.visual_step:self.publish_visual(frame,at,{self.visual_step['id']:self.result})
        except Exception as exc:self.result={'error':str(exc)}
        finally:
            if detector is not None:detector.close()
            if inspector is not None and hasattr(inspector,'close'):inspector.close()


class FrameInspections(FrameInspection):
    """One detector pass and one classification batch for every pending target."""
    def __init__(self,data_dir,profile,catalog,placement,steps,*,linear_state_reader=None,fixed_jigs=None,prepared_inspector=None,geometry_steps=None,geometry_placement=None,timing=None,timing_prefix='inspection'):
        self.timing=timing;self.timing_prefix=timing_prefix
        self.context_lock=threading.RLock();self.context_generation=0;self.busy=False
        self.steps=deepcopy(steps);self.active_ids=frozenset(step['id'] for step in steps)
        self.geometry_steps=deepcopy(geometry_steps)
        self.geometry_placement=deepcopy(geometry_placement)
        first=self.steps[0]
        super().__init__(data_dir,profile,catalog,placement,first['inspection'],first['inspection_product'],linear_state_reader=linear_state_reader,fixed_jigs=fixed_jigs,prepared_inspector=prepared_inspector)
    def retain(self,ids):
        with self.context_lock:self.active_ids=frozenset(ids)
    @property
    def accepting_frame(self):
        with self.context_lock:
            return not self.stop.is_set() and not self.busy and self.jobs.empty()
    def update_context(self,fixed_jigs,placement,ids):
        """Replace association inputs without replacing the native model owner."""
        with self.context_lock:
            if self.stop.is_set():raise RuntimeError('종료된 안착 검사 작업자입니다.')
            if self.fixed_jigs is None or fixed_jigs is None:
                raise ValueError('고정 지그 검사에서만 좌표 문맥을 갱신할 수 있습니다.')
            if self.result and self.result.get('error'):
                raise RuntimeError('안착 검사 오류: '+self.result['error'])
            self.fixed_jigs=deepcopy(fixed_jigs);self.placement=deepcopy(placement)
            self.active_ids=frozenset(ids);self.context_generation+=1;self.result=None
            try:self.jobs.get_nowait()
            except queue.Empty:pass
    def submit(self,frame,at):
        # Tag at enqueue time. A context update can also race a dequeued frame;
        # both the start and publication checks below must reject that old job.
        with self.context_lock:
            if self.stop.is_set():return False
            try:self.jobs.get_nowait()
            except queue.Empty:pass
            copied=timed(self.timing,self.timing_prefix+'.frame_copy',frame.copy)
            try:self.jobs.put_nowait((copied,at,time.monotonic(),self.context_generation))
            except queue.Full:return False
            return True
    def close(self):
        # Serialize cancellation with the result publication check, but release
        # the context lock before joining a worker that still needs it to exit.
        with self.context_lock:self.stop.set()
        super().close()
    def run(self):
        inspector=None;detector=None
        setup_started=time.monotonic()
        try:
            from .ai_inspection import inspector_for
            from .vision_service import MultiDetector
            from .inspection_geometry import anchors_for,inspection_context
            from .occupied_pallet import locate
            inspector=self.prepared_inspector.borrow() if self.prepared_inspector is not None else inspector_for(self.profile)
            inspector.stop=self.stop
            options={'compute':self.prepared_inspector.compute} if getattr(self.prepared_inspector,'compute',None) is not None else {}
            detector=MultiDetector(self.catalog,self.profile,**options) if self.fixed_jigs is None else None
            from .episode_inspection import fixed_jig_results
            fixed=fixed_jig_results(self.fixed_jigs) if self.fixed_jigs is not None else {}
            reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
            if self.geometry_steps and hasattr(inspector,'prepare_geometry'):
                import numpy as np
                size=self.profile.get('intrinsics',{}).get('size',[])
                if len(size)==2 and all(type(v) is int and v>0 for v in size):
                    # Only the dimensions are consulted by anchors_for. This
                    # synthetic frame is never classified, queued or counted.
                    shape=(size[1],size[0],3);empty=np.empty(shape,dtype=np.uint8)
                    warm=[]
                    for station in {s['inspection']['station'] for s in self.geometry_steps}:
                        candidates,_=anchors_for(empty,{},fixed,self.catalog.items,self.profile,reference,STATIONS[station],'전체',self.geometry_placement or self.placement)
                        warm.extend(a for a in candidates if a['inspection_allowed'] and any(
                            s['inspection']['station']==station and target_anchor(a,s['inspection'],s['inspection_product'])
                            for s in self.geometry_steps))
                    timed(self.timing,self.timing_prefix+'.geometry_prepare',inspector.prepare_geometry,warm,self.profile,shape)
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            if self.timing:self.timing.add(self.timing_prefix+'.worker_setup',time.monotonic()-setup_started)
            while not self.stop.is_set():
                try:job=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                if job is None or self.stop.is_set():break
                frame,at,queued,generation=job
                with self.context_lock:
                    if generation!=self.context_generation or self.stop.is_set():continue
                    steps=[s for s in self.steps if s['id'] in self.active_ids]
                    fixed_jigs=self.fixed_jigs;placement=self.placement
                    self.busy=True
                try:
                    if self.timing:
                        self.timing.add(self.timing_prefix+'.frame_queue_wait',time.monotonic()-queued)
                        self.timing.add(self.timing_prefix+'.frame_age_at_start',time.monotonic()-at)
                    if not steps:continue
                    fixed=fixed_jig_results(fixed_jigs) if fixed_jigs is not None else {}
                    fresh,inference=timed(self.timing,self.timing_prefix+'.jig_detect',detect_inspection_frame,detector,frame,self.profile,inspector)
                    stations={s['inspection']['station'] for s in steps}
                    if fixed_jigs is None and 'finished' in stations:
                        jid=reference['stations']['finished_pallet']['jig_id'];config=self.catalog.items.get(jid)
                        # Preserve the standard empty-pallet pose. Occupied recovery
                        # supplies inspection anchors only, never robot target poses.
                        observed=locate(frame,config,self.profile) if config and not fresh.get(jid,{}).get('selected') else None
                        if observed is not None:fresh[jid]=observed
                    located,surrounding=timed(self.timing,self.timing_prefix+'.anchors',inspection_context,
                        frame,fresh,fixed,self.catalog.items,self.profile,reference,placement)
                    selected={};out={};anchors={}
                    for step in steps:
                        check=step['inspection'];candidates,reason=located[check['station']]
                        chosen=[a for a in candidates if target_anchor(a,check,step['inspection_product'])]
                        out[step['id']]={'at':at,'row':None,'reason':reason}
                        if len(chosen)==1 and chosen[0]['inspection_allowed']:
                            selected[step['id']]=chosen[0]['id'];anchors[chosen[0]['id']]=chosen[0]
                    classified=[];self.frame_evidence_ref=None
                    evidence_context={'generation':generation,'steps':steps,'fixed_jigs':fixed_jigs,'placement':placement}
                    if anchors:
                        classified=timed(self.timing,self.timing_prefix+'.inspect',self.classify,inspector,frame,at,list(anchors.values()),
                                         context_anchors=surrounding,inference=inference,metadata=evidence_context)
                    else:self.frame_evidence_ref=self.evidence_frame(frame,at,metadata={**evidence_context,'results':out,'mask_state':'not_requested'})
                    rows={r['id']:r for r in classified}
                    for key,anchor_id in selected.items():out[key]['row']=deepcopy(rows.get(anchor_id))
                    for key,value in out.items():self.evidence_result(self.frame_evidence_ref,value,key)
                    with self.context_lock:
                        if self.frame_evidence_ref is not None:
                            recorder=getattr(self.prepared_inspector,'evidence',None)
                            if recorder is not None:recorder.note(self.frame_evidence_ref,{'kind':'inspection_publication',
                                'published':generation==self.context_generation and not self.stop.is_set(),
                                'generation':generation,'current_generation':self.context_generation,'cancelled':self.stop.is_set()})
                        if generation==self.context_generation and not self.stop.is_set():
                            self.result={'at':at,'results':out,'completed':time.monotonic()}
                            self.publish_visual(frame,at,out)
                except Exception as exc:
                    with self.context_lock:
                        # A retired context's failure is no more current than
                        # its successful result. Current-context errors still
                        # terminate the worker and fail the caller closed.
                        if generation==self.context_generation and not self.stop.is_set():
                            self.result={'error':str(exc)}
                            return
                finally:
                    with self.context_lock:self.busy=False
        except Exception as exc:self.result={'error':str(exc)}
        finally:
            if detector is not None:detector.close()
            if inspector is not None and hasattr(inspector,'close'):inspector.close()
