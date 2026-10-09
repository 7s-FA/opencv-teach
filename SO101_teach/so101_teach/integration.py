"""Plain A/B episode commands. Application control stays on the Tk thread."""
from copy import deepcopy
from pathlib import Path
import hashlib,json,time,uuid
from .domain import JOINTS,atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


class EpisodeCoordinator:
    def __init__(self,apps,data_dir,config,*,emit=None,clock=time.monotonic):
        self.apps=apps;self.data_dir=Path(data_dir);self.config=deepcopy(config);self.clock=clock
        self.emit=emit or (lambda arm,text:None);self.active=None;self.pi_job=None;self.enabled=True
        self.folder=self.data_dir/'integration-runs';self.folder.mkdir(parents=True,exist_ok=True)
        # Incomplete runs remain evidence, never a queue to replay on startup.
        for path in self.folder.glob('*.json'):
            record=json.loads(path.read_text())
            if record['state'] not in ('DONE','FAILED','STOPPED','UNKNOWN'):
                record.update(state='UNKNOWN',detail='수신부 재시작 · 이전 실행 완료 여부 미확인')
                atomic_json(path,record)

    def report(self,arm,command,state,detail=''):
        text=f'{command}_{state}'+(':'+str(detail).replace('\n',' ')[:300] if detail else '')
        self.emit(arm,text);return text

    def persist(self):
        if self.active:atomic_json(self.folder/(self.active['run_id']+'.json'),self.active)

    def sample(self,arm):
        a=self.apps[arm];s=a.session
        return (getattr(s,'latest',None) or a.latest) if s else None

    def stopped(self,arm):
        s=self.apps[arm].session;p=self.sample(arm)
        return bool(s and s.running and not s.error and s.state in ('HOLD','READ_ONLY') and p and p.fresh()
                    and p.calibration_matches and all(p.telemetry.get(n,{}).get('moving')==0 for n in JOINTS))

    def busy(self):
        for a in self.apps.values():
            job=getattr(getattr(a,'workspace_manager',None),'pi_execution',None)
            if job and job.busy:return True
            s=a.session
            if (getattr(getattr(a,'inspection_run',None),'busy',False) or a.closed or a.playing or a.pending_execution or a.camera_task or a.safe_entry or a.remote_plan_job
                    or a.live_adjust.owner is not None or a.settings.worker and a.settings.worker.running
                    or s and (s.state not in ('HOLD','READ_ONLY') or s.command_pending.is_set() or s.program_active.is_set())):return True
        return False

    def load_recipe(self,arm,command):
        mapping=self.config['recipes'].get(arm,{}).get(command)
        if not mapping:raise ValueError('UNKNOWN_COMMAND')
        episode_id=mapping['episode_id']
        if not isinstance(episode_id,str) or len(episode_id)!=32 or any(c not in '0123456789abcdef' for c in episode_id):raise ValueError('INVALID_EPISODE_ID')
        a=self.apps[arm];doc=a.store.load(self.data_dir/'episodes'/(episode_id+'.json'))
        if doc.get('robot_id','arm2')!=arm:raise ValueError('EPISODE_ARM_MISMATCH')
        if not doc['steps']:raise ValueError('EMPTY_EPISODE')
        timeout=mapping['timeout_seconds']
        if type(timeout) not in (int,float) or not 10<=timeout<=3600:raise ValueError('INVALID_RECIPE_TIMEOUT')
        return doc,float(timeout)

    def command(self,arm,text):
        command=text.strip()
        try:
            if arm not in self.apps:raise ValueError('UNKNOWN_ARM')
            if command=='STOP':
                if self.active and self.active['arm']==arm:self.cancel('OPERATOR_STOP')
                else:self.report(arm,command,'REJECTED','NO_ACTIVE_EPISODE')
                return
            if not self.enabled:raise ValueError('RECEIVER_DISABLED')
            doc,timeout=self.load_recipe(arm,command)
            if self.active or self.busy():raise ValueError('BUSY')
            a=self.apps[arm];s=a.session;p=self.sample(arm)
            if not s or not s.running or s.error or s.state!='HOLD' or not p or not p.fresh() or not p.calibration_matches or p.role!='follower':raise ValueError('FOLLOWER_NOT_READY')
            if getattr(a.remote,'error',None):raise ValueError('REMOTE_NOT_READY')
            current_path=self.data_dir/'episodes'/(a.episode['id']+'.json')
            if a.episode.get('steps') and (not current_path.exists() or digest(a.episode)!=digest(a.store.load(current_path))):raise ValueError('UNSAVED_EPISODE')
            record={'run_id':uuid.uuid4().hex,'arm':arm,'command':command,'episode_id':doc['id'],'episode_name':doc['name'],
                    'episode_sha256':digest(doc),'state':'ACCEPTED','phase':'STARTING','started':False,
                    'request_id':None,'final_request_id':None,'created_at':time.time()}
            # Persist acceptance before any physical command, and never auto-replay.
            atomic_json(self.folder/(record['run_id']+'.json'),record)
            self.active=record;self.deadline=self.clock()+timeout;self.report(arm,command,'ACCEPTED')
            try:
                a.episode=deepcopy(doc);a.selected=None;a.episode_name.set(doc['name']);a.follow_jig.set(False)
                a.refresh_steps();a.refresh_library();a.execute_episode()  # Exactly the GUI episode button path.
                self.pi_job=getattr(getattr(a,'workspace_manager',None),'pi_execution',None)
            except Exception as exc:self.cancel('START_FAILED: '+str(exc))
        except Exception as exc:self.report(arm,command or '?','REJECTED',str(exc))

    def submitted(self,arm,action,request_id,targets):
        r=self.active
        if not r or r['arm']!=arm or r['state']=='STOPPING' or action not in ('move','play'):return
        r['request_id']=request_id
        if action=='play':r['final_request_id']=request_id;r['final_ticks']=deepcopy(targets[-1])
        r['state']='RUNNING';r['phase']='RUNNING' if action=='play' else 'SAFE_ENTRY';self.persist()

    def started(self):
        if self.active['started']:return
        self.active['started']=True;self.persist()
        self.report(self.active['arm'],self.active['command'],'STARTED')

    def finish(self,state,detail=''):
        r=self.active
        r.update(state=state,detail=detail,finished_at=time.time(),stopped=self.stopped(r['arm']))
        p=self.sample(r['arm']);r['final_actual_ticks']=p.ticks.copy() if p else None
        self.persist();self.active=None;self.pi_job=None;self.report(r['arm'],r['command'],state,detail)

    def cancel(self,reason='OPERATOR_STOP'):
        if not self.active or self.active['state']=='STOPPING':return
        r=self.active;r.update(state='STOPPING',phase='STOPPING',stop_reason=reason);self.persist();self.stop_requested_at=self.clock()
        try:self.apps[r['arm']].motion_request('hold')
        except Exception as exc:self.finish('FAILED','STOP_UNCONFIRMED: '+str(exc))

    def poll(self):
        if not self.active:return
        r=self.active;a=self.apps[r['arm']];s=a.session;now=self.clock()
        if r['state']=='STOPPING':
            if self.pi_job and self.pi_job.busy:return
            if self.stopped(r['arm']):self.finish('STOPPED' if r['stop_reason']=='OPERATOR_STOP' else 'FAILED',r['stop_reason'])
            elif now-self.stop_requested_at>5:self.finish('FAILED','STOP_UNCONFIRMED')
            return
        if now>self.deadline:self.cancel('TASK_TIMEOUT');return
        if not s or not s.running or s.error or getattr(a.remote,'error',None):self.cancel('DEVICE_ERROR');return
        if a.episode['id']!=r['episode_id']:self.cancel('EPISODE_CHANGED');return
        if self.pi_job:
            if self.pi_job.busy:
                if self.pi_job.record['state']=='running':self.started()
                return
            if self.pi_job.record['state']=='done':
                if self.stopped(r['arm']):self.started();self.finish('DONE')
            else:self.finish('FAILED',self.pi_job.record.get('error') or 'PI_EXECUTION_STOPPED')
            return
        if s.state=='MOVING' and s.active_request_id==r['request_id']:self.started()
        inspection=getattr(a,'inspection_run',None)
        if inspection and inspection.phase=='failed':self.cancel('INSPECTION_FAILED: '+str(inspection.error));return
        if inspection and inspection.busy:r['phase']='INSPECTING' if inspection.phase=='inspection' else 'RUNNING';return
        if a.safe_entry:r['phase']='SAFE_ENTRY';return
        if a.pending_execution:r['phase']='MEASURING';return
        if a.remote_plan_job:r['phase']='PLANNING';return
        if s.state=='MOVING' or s.command_pending.is_set() or s.program_active.is_set():return
        final=r['final_request_id']
        if final is not None and s.completed_request_id==final:
            if self.stopped(r['arm']):self.started();self.finish('DONE')
            # The final result can arrive before the next fresh stopped sample.
            return
        self.finish('FAILED',getattr(a,'last_motion_stop',None) or a.message.get() or 'EXECUTION_STOPPED')
