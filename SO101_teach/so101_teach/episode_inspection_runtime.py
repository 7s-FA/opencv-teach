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
            try:passed=decision.observe(worker.result,now)
            except ValueError as exc:raise ValueError(item['step']['name']+' · '+str(exc)) from exc
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
    def __init__(self,data_dir,profile,catalog,placement,check,product,*,linear_state_reader=None,fixed_jigs=None):
        require_resources()
        # OpenCV must finish its current call before Python tears down native state.
        # Cancellation still returns promptly after signalling stop.
        super().__init__(daemon=False,name='episode-seating-check')
        self.data_dir=Path(data_dir);self.profile=deepcopy(profile);self.placement=deepcopy(placement);self.check=deepcopy(check);self.product=product
        self.linear_state_reader=linear_state_reader;self.fixed_jigs=deepcopy(fixed_jigs)
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
        try:
            from .shape_inspection import ShapeInspector
            from .vision_service import MultiDetector
            from .inspection_geometry import anchors_for
            from .occupied_pallet import locate
            inspector=ShapeInspector();inspector.stop=self.stop
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            detector=MultiDetector(self.catalog,self.profile) if self.fixed_jigs is None else None
            from .episode_inspection import fixed_jig_results
            fixed=fixed_jig_results(self.fixed_jigs) if self.fixed_jigs is not None else {}
            reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
            tracker=None
            if self.fixed_jigs is None and (self.data_dir/'inspection_reference/pallet.json').exists():
                from .inspection_tracking import PalletReference
                tracker=PalletReference(self.data_dir/'inspection_reference')
            while not self.stop.is_set():
                try:frame,at=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                fresh=detector.process(frame)['live_by_jig'] if detector else {}
                if self.fixed_jigs is None and self.check['station']=='finished':
                    jid=reference['stations']['finished_pallet']['jig_id'];config=self.catalog.items.get(jid)
                    observed=tracker.locate(frame,self.profile,config) if tracker and config else None
                    if observed is None and not fresh.get(jid,{}).get('selected') and config:observed=locate(frame,config,self.profile)
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
                rows=inspector.inspect(frame,chosen,self.profile)
                self.result={'at':at,'row':deepcopy(rows[0]) if len(rows)==1 else None,'reason':reason,'completed':time.monotonic()}
        except Exception as exc:self.result={'error':str(exc)}


class FrameInspections(FrameInspection):
    """One detector pass and one classification batch for every pending target."""
    def __init__(self,data_dir,profile,catalog,placement,steps,*,linear_state_reader=None,fixed_jigs=None):
        self.steps=deepcopy(steps);self.active_ids=frozenset(step['id'] for step in steps)
        first=self.steps[0]
        super().__init__(data_dir,profile,catalog,placement,first['inspection'],first['inspection_product'],linear_state_reader=linear_state_reader,fixed_jigs=fixed_jigs)
    def retain(self,ids):self.active_ids=frozenset(ids)
    def run(self):
        try:
            from .shape_inspection import ShapeInspector
            from .vision_service import MultiDetector
            from .inspection_geometry import anchors_for
            from .occupied_pallet import locate
            inspector=ShapeInspector();inspector.stop=self.stop
            detector=MultiDetector(self.catalog,self.profile) if self.fixed_jigs is None else None
            from .episode_inspection import fixed_jig_results
            fixed=fixed_jig_results(self.fixed_jigs) if self.fixed_jigs is not None else {}
            reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
            tracker=None
            if self.fixed_jigs is None and (self.data_dir/'inspection_reference/pallet.json').exists():
                from .inspection_tracking import PalletReference
                tracker=PalletReference(self.data_dir/'inspection_reference')
            if self.linear_state_reader:
                if not self.placement:raise ValueError('시작 전 검사: 작업대 좌표 없음')
                self.placement['linear_stage']['startup_state']=self.linear_state_reader()
            while not self.stop.is_set():
                try:frame,at=self.jobs.get(timeout=.1)
                except queue.Empty:continue
                steps=[s for s in self.steps if s['id'] in self.active_ids]
                if not steps:continue
                fresh=detector.process(frame)['live_by_jig'] if detector else {}
                stations={s['inspection']['station'] for s in steps}
                if self.fixed_jigs is None and 'finished' in stations:
                    jid=reference['stations']['finished_pallet']['jig_id'];config=self.catalog.items.get(jid)
                    observed=tracker.locate(frame,self.profile,config) if tracker and config else None
                    if observed is None and not fresh.get(jid,{}).get('selected') and config:observed=locate(frame,config,self.profile)
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
                rows={r['id']:r for r in inspector.inspect(frame,list(anchors.values()),self.profile)} if anchors else {}
                for key,anchor_id in selected.items():out[key]['row']=deepcopy(rows.get(anchor_id))
                self.result={'at':at,'results':out,'completed':time.monotonic()}
        except Exception as exc:self.result={'error':str(exc)}
