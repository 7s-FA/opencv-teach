"""Fresh-frame inspection worker shared by PC and Pi checkpoint execution."""
from copy import deepcopy
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from .domain import ROOT
from .episode_inspection import STATIONS,target_anchor


class PreparedInspector:
    """One execution owns the model; observations and cancellation stay per worker."""
    def __init__(self,profile):
        require_resources()
        self.profile=deepcopy(profile)
        self.lock=threading.RLock();self.ready=threading.Event();self.closed=threading.Event()
        self.inspector=None;self.error=None
        self.thread=threading.Thread(target=self._prepare,daemon=False,name='inspection-model-prepare')
        self.thread.start()
    def _prepare(self):
        try:
            from .ai_inspection import inspector_for,inspection_settings
            import numpy as np
            self.inspector=inspector_for(self.profile)
            if hasattr(self.inspector,'provider') and not self.closed.is_set():
                # Synthetic warm-up is discarded. It cannot enter any decision,
                # jig latch, frame queue, geometry cache or inspection result.
                self.inspector.provider.prepare(np.zeros((480,640,3),np.uint8),inspection_settings(self.profile))
        except Exception as exc:self.error=exc
        finally:self.ready.set()
    def borrow(self):
        owner=self
        class BorrowedInspector:
            stop=None
            def inspect(self,frame,anchors,profile,*,inference=None):
                return owner.inspect(frame,anchors,profile,self.stop,inference=inference)
            def predict_all(self,frame,settings):
                return owner._call(owner.profile,self.stop,'predict_all',frame,settings)
            def prepare_geometry(self,anchors,profile,shape):
                return owner._call(profile,self.stop,'prepare_geometry',anchors,profile,shape)
            def close(self):pass
        return BorrowedInspector()
    def inspect(self,frame,anchors,profile,stop,*,inference=None):
        # Acquire frame evidence before the inspector lock. Its predictor may
        # borrow this same prepared model from another consumer/thread.
        if inference is not None and anchors and not self.closed.is_set() and not (stop is not None and stop.is_set()):
            from .ai_inspection import inspection_settings
            inference.get(frame,inspection_settings(profile))
        options={'inference':inference} if inference is not None else {}
        return self._call(profile,stop,'inspect',frame,anchors,profile,**options)
    def _call(self,profile,stop,method,*args,**kwargs):
        def cancelled():return self.closed.is_set() or stop is not None and stop.is_set()
        while not self.ready.wait(.05):
            if cancelled():return []
        if cancelled():return []
        if self.error is not None:raise RuntimeError('안착 검사 모델 준비 실패: '+str(self.error)) from self.error
        if profile!=self.profile:raise ValueError('준비된 안착 검사 프로필과 현재 프로필이 다릅니다.')
        while not self.lock.acquire(timeout=.05):
            if cancelled():return []
        try:
            if cancelled():return []
            self.inspector.stop=stop
            if method=='prepare_geometry' and not hasattr(self.inspector,method):return
            return getattr(self.inspector,method)(*args,**kwargs)
        finally:self.lock.release()
    def close(self):
        self.closed.set();self.thread.join()
        with self.lock:
            if self.inspector is not None:
                if hasattr(self.inspector,'close'):self.inspector.close()
                self.inspector=None


class InspectionBatch:
    """Poll independent checks while the caller continues its motion program."""
    def __init__(self,product,make_worker,on_pass,*,evidence=None):
        from .assembly_evidence import AssemblyEvidence
        self.evidence=evidence if evidence is not None else AssemblyEvidence()
        self.product=product;self.make_worker=make_worker;self.on_pass=on_pass;self.pending=[]
    def start(self,step):
        from .episode_inspection import InspectionDecision
        now=time.monotonic()
        worker=self.make_worker(step)
        self.pending.append({'step':deepcopy(step),'worker':worker,'decision':InspectionDecision(step['inspection'],self.product,now,evidence=self.evidence),'submitted':None})
    def poll(self,view=None):
        now=time.monotonic()
        for item in list(self.pending):
            decision=item['decision'];worker=item['worker']
            if view and decision.started<=view[1]<=now and now-view[1]<1 and view[1]!=item['submitted']:
                worker.submit(view[0],view[1]);item['submitted']=view[1]
            observation=worker.result
            try:passed=decision.observe(observation,now)
            except ValueError as exc:
                failure=ValueError(item['step']['name']+' · '+str(exc))
                failure.inspection_snapshot={'steps':[deepcopy(item['step'])],'result':deepcopy(observation),'result_role':'failure_decision_input'}
                raise failure from exc
            if passed:
                step=item['step']
                if decision.basis:step['inspection_basis']=deepcopy(decision.basis)
                self.evidence.record(step,decision.product)
                self.on_pass(step);worker.close();self.pending.remove(item)
    def restart(self):
        steps=[item['step'] for item in self.pending];self.close()
        for step in steps:self.start(step)
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
    def __init__(self,data_dir,profile,catalog,placement,check,product,*,linear_state_reader=None,fixed_jigs=None,prepared_inspector=None):
        require_resources()
        # OpenCV must finish its current call before Python tears down native state.
        # Cancellation still returns promptly after signalling stop.
        super().__init__(daemon=False,name='episode-seating-check')
        self.data_dir=Path(data_dir);self.profile=deepcopy(profile);self.placement=deepcopy(placement);self.check=deepcopy(check);self.product=product
        self.linear_state_reader=linear_state_reader;self.fixed_jigs=deepcopy(fixed_jigs)
        self.prepared_inspector=prepared_inspector
        meshes={key:deepcopy(catalog.mesh(key)) for key in catalog.items} if fixed_jigs is None else {}
        self.catalog=SimpleNamespace(items=deepcopy(catalog.items),revision=0,mesh=lambda key:meshes[key])
        self.stop=threading.Event();self.jobs=queue.Queue(1);self.result=None;self.start()
    def submit(self,frame,at):
        if self.stop.is_set():return
        try:self.jobs.get_nowait()
        except queue.Empty:pass
        self.jobs.put_nowait((frame.copy(),at))
    def close(self):
        self.stop.set()
        if self.is_alive() and threading.current_thread() is not self:self.join(.2)
    def run(self):
        inspector=None;detector=None
        try:
            from .ai_inspection import inspector_for
            from .vision_service import MultiDetector
            from .inspection_geometry import anchors_for
            from .occupied_pallet import locate
            inspector=self.prepared_inspector.borrow() if self.prepared_inspector is not None else inspector_for(self.profile)
            inspector.stop=self.stop
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            detector=MultiDetector(self.catalog,self.profile) if self.fixed_jigs is None else None
            from .episode_inspection import fixed_jig_results
            fixed=fixed_jig_results(self.fixed_jigs) if self.fixed_jigs is not None else {}
            reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
            while not self.stop.is_set():
                try:frame,at=self.jobs.get(timeout=.1)
                except queue.Empty:continue
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
                        self.result={'at':at,'row':None,'reason':'리니어 정지·명령 위치 확인 필요'};continue
                # Compare both product families so selecting A cannot turn B into A.
                anchors,reason=anchors_for(frame,fresh,fixed,self.catalog.items,self.profile,reference,STATIONS[self.check['station']],'전체',self.placement)
                chosen=[a for a in anchors if target_anchor(a,self.check,self.product)]
                if len(chosen)!=1 or not chosen[0]['inspection_allowed']:
                    self.result={'at':at,'row':None,'reason':('실행 전 확정 지그 위치 없음 · '+reason) if self.fixed_jigs is not None else reason};continue
                rows=inspector.inspect(frame,chosen,self.profile,inference=inference) if inference is not None else inspector.inspect(frame,chosen,self.profile)
                self.result={'at':at,'row':deepcopy(rows[0]) if len(rows)==1 else None,'reason':reason,'completed':time.monotonic()}
        except Exception as exc:self.result={'error':str(exc)}
        finally:
            if detector is not None:detector.close()
            if inspector is not None and hasattr(inspector,'close'):inspector.close()


class FrameInspections(FrameInspection):
    """One detector pass and one classification batch for every pending target."""
    def __init__(self,data_dir,profile,catalog,placement,steps,*,linear_state_reader=None,fixed_jigs=None,prepared_inspector=None,geometry_steps=None,geometry_placement=None):
        self.steps=deepcopy(steps);self.active_ids=frozenset(step['id'] for step in steps)
        self.geometry_steps=deepcopy(geometry_steps)
        self.geometry_placement=deepcopy(geometry_placement)
        first=self.steps[0]
        super().__init__(data_dir,profile,catalog,placement,first['inspection'],first['inspection_product'],linear_state_reader=linear_state_reader,fixed_jigs=fixed_jigs,prepared_inspector=prepared_inspector)
    def retain(self,ids):self.active_ids=frozenset(ids)
    def run(self):
        inspector=None;detector=None
        try:
            from .ai_inspection import inspector_for
            from .vision_service import MultiDetector
            from .inspection_geometry import anchors_for
            from .occupied_pallet import locate
            inspector=self.prepared_inspector.borrow() if self.prepared_inspector is not None else inspector_for(self.profile)
            inspector.stop=self.stop
            detector=MultiDetector(self.catalog,self.profile) if self.fixed_jigs is None else None
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
                    inspector.prepare_geometry(warm,self.profile,shape)
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            while not self.stop.is_set():
                try:frame,at=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                steps=[s for s in self.steps if s['id'] in self.active_ids]
                if not steps:continue
                fresh,inference=detect_inspection_frame(detector,frame,self.profile,inspector)
                stations={s['inspection']['station'] for s in steps}
                if self.fixed_jigs is None and 'finished' in stations:
                    jid=reference['stations']['finished_pallet']['jig_id'];config=self.catalog.items.get(jid)
                    # Preserve the standard empty-pallet pose. Occupied recovery
                    # supplies inspection anchors only, never robot target poses.
                    observed=locate(frame,config,self.profile) if config and not fresh.get(jid,{}).get('selected') else None
                    if observed is not None:fresh[jid]=observed
                located={}
                for station in stations:
                    if station=='linear':
                        state=(self.placement or {}).get('linear_stage',{}).get('startup_state',{})
                        if not state.get('known') or state.get('moving') or state.get('pending'):
                            located[station]=([], '리니어 정지·명령 위치 확인 필요');continue
                    located[station]=anchors_for(frame,fresh,fixed,self.catalog.items,self.profile,reference,STATIONS[station],'전체',self.placement)
                selected={};out={};anchors={}
                for step in steps:
                    check=step['inspection'];candidates,reason=located[check['station']]
                    chosen=[a for a in candidates if target_anchor(a,check,step['inspection_product'])]
                    out[step['id']]={'at':at,'row':None,'reason':reason}
                    if len(chosen)==1 and chosen[0]['inspection_allowed']:
                        selected[step['id']]=chosen[0]['id'];anchors[chosen[0]['id']]=chosen[0]
                classified=[]
                if anchors:
                    classified=(inspector.inspect(frame,list(anchors.values()),self.profile,inference=inference)
                                if inference is not None else inspector.inspect(frame,list(anchors.values()),self.profile))
                rows={r['id']:r for r in classified}
                for key,anchor_id in selected.items():out[key]['row']=deepcopy(rows.get(anchor_id))
                self.result={'at':at,'results':out,'completed':time.monotonic()}
        except Exception as exc:self.result={'error':str(exc)}
        finally:
            if detector is not None:detector.close()
            if inspector is not None and hasattr(inspector,'close'):inspector.close()
