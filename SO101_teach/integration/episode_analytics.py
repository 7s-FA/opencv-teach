"""Versioned JSON execution evidence. In-memory only; never queries or controls devices."""
from copy import deepcopy
import base64
import hashlib
import json
import math
import threading
import time


def json_value(value):
    """Keep numerical precision, with JSON null for missing/non-finite observations."""
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):return value if math.isfinite(value) else None
    if isinstance(value,dict):return {str(k):json_value(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_value(v) for v in value]
    return None


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


class ExecutionAnalysis:
    def __init__(self,job_id,started):
        self.lock=threading.RLock();self.started=started
        self.data={'schema_version':2,'job_id':job_id,'operation':None,'request_id':None,'source':'unknown',
                   'category':'unknown','outcome':'in_progress','record_complete':False,
                   'runs':[],'configurations':{},'active_contexts':{},'jig_pose_sets':[],
                   'plans':[],'motion_calls':[],'follower_observations':[],
                   'inspection_by_arm':{},'expected_inspections':{},'control_requests':{},'artifacts':{},
                   'pause_intervals':[],'linear_intervals':[],'linear_requests':[],
                   'preparation_profiles':{},'readiness_checks':[],'resource_preparation':[],'inspection_attempts':[],
                   'step_arrivals':[],'arrival_transport_gaps':[],'event_count':0,
                   'controls':{'stop_requests':0,'resumes':0,'resets':0},'failure':None,
                   'clock':{'duration_origin':'executor_start','duration_clock':'monotonic',
                            'cross_host_clock_subtraction_allowed':False},
                   'measurement_notes':{'arm_time':'controller-call interval, not measured motor-moving duration',
                                        'linear_position':'commanded target only; no position sensor verification',
                                        'overlap':'inspection, preparation, arm and linear intervals may overlap'}}
        self.active_pose={};self.active_plan={};self.last_follower={};self.last_step={};self.cancelled=False;self.execution_started=False
        self.controller_sessions={};self.arrival_cursors={};self.arrival_ids=set()

    def decision_evidence(self,reference):
        with self.lock:
            refs=self.data['artifacts'].setdefault('decision_evidence',[])
            value=json_value(reference)
            if value not in refs:refs.append(value)

    def preparation_profile(self,profile):
        """A profile is cumulative across arm handoff; replace, never add it."""
        key=profile.get('profile_id')
        if key:
            with self.lock:
                self.data['preparation_profiles'][key]=json_value({**profile,
                    'job_origin_offset_s':profile['origin_monotonic_s']-self.started})

    def resource_prepared(self,arm,resource,action):
        with self.lock:
            self.data['resource_preparation'].append({'arm':arm,'resource':resource,'action':action,
                'observed_elapsed_s':self.elapsed(),
                'note':'Instance creation/reuse observation; readiness is checked separately.'})

    def _arrivals(self,arm,state):
        evidence=state.get('arrival_evidence') or {};session=evidence.get('controller_session_id')
        if not session:return
        self.controller_sessions[arm]=session
        rows=evidence.get('arrivals') or [];cursor_key=(arm,session)
        previous=self.arrival_cursors.get(cursor_key)
        seqs=[r['arrival_seq'] for r in rows if type(r.get('arrival_seq')) is int]
        if previous is not None and seqs and min(seqs)>previous+1:
            self.data['arrival_transport_gaps'].append({'arm':arm,'controller_session_id':session,
                'after_seq':previous,'first_received_seq':min(seqs)})
        self.arrival_cursors[cursor_key]=max([previous or 0,*seqs])
        for row in rows:
            if row.get('controller_session_id')!=session or type(row.get('arrival_seq')) is not int:continue
            key=(arm,session,row['arrival_seq'])
            if key in self.arrival_ids:continue
            matches=[(m,r) for m in self.data['motion_calls'] if m['arm']==arm for r in m['requests']
                     if r.get('controller_session_id')==session and r['request_id']==row.get('request_id')]
            if len(matches)!=1:continue
            motion,request=matches[0];index=row.get('step_index')
            if type(index) is not int or not 1<=index<=len(motion['targets_ticks'])-request['step_offset']:continue
            original_index=request['step_offset']+index;target=motion['targets_ticks'][original_index-1]
            # Gripper targets can legitimately change at contact. Preserve both
            # targets rather than dropping these arrivals or calling them errors.
            if any(row.get('target',{}).get(k)!=v for k,v in target.items() if k!='gripper'):continue
            self.arrival_ids.add(key)
            self.data['step_arrivals'].append(json_value({**row,'arm':arm,
                'motion_id':motion['motion_id'],'original_step_index':original_index,
                'planned_target':target,'observed_elapsed_s':self.elapsed(),
                'source':'controller_arrival','error_basis':'encoder_minus_executed_target; not external TCP accuracy'}))

    def elapsed(self):return max(0.,time.monotonic()-self.started)

    def configure(self,operation,job_id,started,*,request_id=None,source='cli',execution_started=True):
        with self.lock:
            self.started=started;self.execution_started=execution_started
            self.data.update(operation=operation,job_id=job_id,request_id=request_id,source=source,
                category='production' if operation.startswith(('build_','load_')) else 'linear' if operation.startswith('slide_') else 'recovery')

    def register_run(self,run_id,arm):
        with self.lock:
            if not any(r['run_id']==run_id for r in self.data['runs']):
                self.data['runs'].append({'run_id':run_id,'arm':arm,'first_event_elapsed_s':None,'phase_completed':False})

    def context(self,arm,episode,bundle,speed,target,timeout_seconds=None):
        calibration={}
        for name,encoded in bundle.get('files',{}).items():
            if name.startswith('calibration/') and name.endswith('.json'):
                try:calibration[name]=json.loads(base64.b64decode(encoded,validate=True))
                except (ValueError,TypeError,UnicodeError):calibration[name]=None
        config=json_value({'arm':arm,'episode':episode,'profile':bundle.get('profile'),
            'jigs':bundle.get('jigs'),'mesh_hashes':bundle.get('mesh_hashes'),
            'calibration':calibration,'trim_ticks':bundle.get('trim_ticks'),
            'pose_acquisition':{k:bundle.get(k) for k in ('hold_seconds','acquisition_seconds','acquisition_attempts')},
            'speed_ticks_s':speed,'linear_target_mm':target,'runner_timeout_seconds':timeout_seconds})
        key=digest(config)
        with self.lock:
            self.data['configurations'].setdefault(key,config);self.data['active_contexts'][arm]=key
        return key

    def poses(self,arm,current,observations,linear_target):
        with self.lock:
            key=f"{arm}:poses:{len(self.data['jig_pose_sets'])+1}"
            rows=[]
            for jig_id,item in current.items():
                pose=item.get('pose') or [None,None,None];observation=observations.get(jig_id,{})
                rows.append({'jig_id':jig_id,'x_mm':pose[0],'y_mm':pose[1],'yaw_deg':pose[2],
                    'z_mm':None,'coordinate_frame':'profile_world','robot_id':arm,
                    'symmetry_deg':item.get('symmetry_deg'),'mesh_sha256':item.get('stl_sha256'),
                    'pose_measured_monotonic_s':observation.get('pose_measured_at'),
                    'acquisition_completed_attempts':observation.get('acquisition_completed_attempts'),
                    'quad':(observation.get('selected') or {}).get('quad'),
                    'outline_px':(observation.get('selected') or {}).get('outline_px'),
                    'center_px':(observation.get('selected') or {}).get('center_px'),
                    'metric':(observation.get('selected') or {}).get('metric')})
            context=self.data['configurations'].get(self.data['active_contexts'].get(arm),{})
            self.data['jig_pose_sets'].append(json_value({'pose_set_id':key,'arm':arm,'adopted_elapsed_s':self.elapsed(),
                'image_size':(context.get('profile',{}).get('intrinsics') or {}).get('size'),
                'context_id':self.data['active_contexts'].get(arm),'linear_target_mm':linear_target,
                'linear_position_measured':False,'poses':rows}))
            self.active_pose[arm]=key
        return key

    def plan(self,arm,steps,plan):
        with self.lock:
            key=f'plan:{len(self.data["plans"])+1}'
            self.data['plans'].append(json_value({'plan_id':key,
                'arm':arm,'adopted_elapsed_s':self.elapsed(),'context_id':self.data['active_contexts'].get(arm),
                'pose_set_id':self.active_pose.get(arm),'steps':steps,'plan':plan}))
            self.active_plan[arm]=key

    def motion_start(self,arm,action,targets):
        with self.lock:
            token=len(self.data['motion_calls'])
            self.data['motion_calls'].append(json_value({'motion_id':token,'arm':arm,'action':action,
                'context_id':self.data['active_contexts'].get(arm),'pose_set_id':self.active_pose.get(arm),
                'plan_id':self.active_plan.get(arm) if action=='play' else None,
                'started_elapsed_s':self.elapsed(),'finished_elapsed_s':None,'outcome':'in_progress',
                'requests':[],'send_attempts':[],'targets_ticks':targets}))
            return token

    def motion_attempt(self,token,step_offset=0):
        if token is None:return
        with self.lock:
            self.data['motion_calls'][token]['send_attempts'].append({
                'sent_elapsed_s':self.elapsed(),'step_offset':step_offset,'request_id':None})

    def motion_request(self,token,request_id,step_offset=0):
        if token is None:return
        with self.lock:
            motion=self.data['motion_calls'][token]
            attempt=next((r for r in reversed(motion['send_attempts'])
                          if r['request_id'] is None and r['step_offset']==step_offset),None)
            if attempt is not None:attempt['request_id']=request_id
            motion['requests'].append({'request_id':request_id,'acknowledged_elapsed_s':self.elapsed(),
                'step_offset':step_offset,'controller_session_id':self.controller_sessions.get(motion['arm'])})

    def motion_end(self,token,error=None):
        with self.lock:
            row=self.data['motion_calls'][token];end=self.elapsed()
            row.update(finished_elapsed_s=end,elapsed_seconds=max(0.,end-row['started_elapsed_s']),
                       outcome='verified_return' if error is None else 'interrupted' if type(error).__name__ in ('ResetRequested','ShutdownRequested','PreparationRestart') else 'failed',
                       error_type=type(error).__name__ if error else None,error_message=str(error) if error else None)

    def step_completed(self,arm,step,index,total):
        with self.lock:
            self.last_step[arm]={'step_id':step.get('id'),'step_name':step.get('name'),
                'index':index,'total':total,'source':'controller_progress_observed',
                'request_id':self.last_follower.get(arm,(None,None))[1]}

    def follower(self,arm,state):
        if not isinstance(state,dict):return
        key=tuple(state.get(k) for k in ('state','request_id','index','completed_request_id','error','running','program_active','command_pending'))
        key+=((state.get('arrival_evidence') or {}).get('controller_session_id'),)
        with self.lock:
            # Arrival evidence may advance even when the public state tuple does
            # not. Consume it before suppressing duplicate state observations.
            self._arrivals(arm,state)
            if self.last_follower.get(arm)==key:return
            self.last_follower[arm]=key;sample=state.get('latest') or {}
            if not isinstance(sample,dict):sample={}
            telemetry=sample.get('telemetry') or {}
            if not isinstance(telemetry,dict):telemetry={}
            self.data['follower_observations'].append(json_value({'arm':arm,'observed_elapsed_s':self.elapsed(),
                'controller_session_id':self.controller_sessions.get(arm),
                'source':'existing_rpc_state','sample_monotonic_s':sample.get('monotonic'),
                'state':state.get('state'),'request_id':state.get('request_id'),'index':state.get('index'),
                'completed_request_id':state.get('completed_request_id'),'error':state.get('error'),
                'running':state.get('running'),'program_active':state.get('program_active'),'command_pending':state.get('command_pending'),
                'actual_ticks':sample.get('ticks'),'motor_moving':{n:h.get('moving') for n,h in telemetry.items() if isinstance(h,dict)}}))

    def inspection(self,arm,records,path,phase):
        with self.lock:
            rows=json_value(records)
            result='not_inspected' if not rows else 'failed' if any(r.get('result')=='FAIL' for r in rows) else 'observed_passes'
            self.data['inspection_by_arm'][arm]={'records':rows,'result':result,'log_path':str(path) if path else None,
                'last_phase':phase,'pose_set_id':self.active_pose.get(arm)}

    def expect_inspections(self,arm,steps):
        with self.lock:
            self.data['expected_inspections'][arm]=json_value([
                {'step_id':s.get('id'),'step_name':s.get('name'),'check':s.get('inspection'),
                 'product':s.get('inspection_product')} for s in steps if s.get('inspection')])

    def control_requests(self,receipts,supervision_lost):
        with self.lock:
            self.data['control_requests'].update(json_value(receipts))
            self.data['supervision_lost']=bool(supervision_lost)

    def error(self,error,phase=None):
        with self.lock:self.data['failure']={'type':type(error).__name__,'message':str(error),'phase':phase}

    def cancel(self):
        with self.lock:self.cancelled=True

    def event(self,arm,run_id,suffix,at,elapsed):
        kind,_,detail=suffix.partition(':')
        with self.lock:
            self.data['event_count']+=1
            run=next(r for r in self.data['runs'] if r['run_id']==run_id)
            if run['first_event_elapsed_s'] is None:run['first_event_elapsed_s']=elapsed
            self.data['last_event_elapsed_s']=elapsed
            self.data.setdefault('executor_started_at_unix',at-elapsed)
            event={'type':kind,'detail':detail or None,'job_event_seq':self.data['event_count'],
                   'event_id':self.data['job_id']+':'+str(self.data['event_count']),
                   'context_id':self.data['active_contexts'].get(arm),
                   'pose_set_id':self.active_pose.get(arm),'plan_id':self.active_plan.get(arm),'elapsed_monotonic_s':elapsed}
            if kind=='STEP_DONE':event['step']=deepcopy(self.last_step.get(arm))
            if kind in ('JIG_DETECTED','JIG_CONFIRMED'):event['jig_id']=detail
            if kind in ('INSPECTION_STARTED','INSPECTION_PASS','INSPECTION_PROVISIONAL_PASS','INSPECTION_INVALIDATED'):
                event['inspection_step_id']=detail.partition(':')[0]
            if kind=='INSPECTION_STARTED':
                self.data['inspection_attempts'].append({'attempt_id':len(self.data['inspection_attempts'])+1,
                    'arm':arm,'step_id':detail.partition(':')[0],'started_elapsed_s':elapsed,
                    'finished_elapsed_s':None,'outcome':'in_progress'})
            elif kind in ('INSPECTION_PASS','INSPECTION_PROVISIONAL_PASS','INSPECTION_INVALIDATED'):
                for check in reversed(self.data['inspection_attempts']):
                    if check['arm']==arm and check['step_id']==detail.partition(':')[0]:
                        check.update(outcome={'INSPECTION_PASS':'passed','INSPECTION_PROVISIONAL_PASS':'provisional',
                                              'INSPECTION_INVALIDATED':'invalidated'}[kind])
                        if kind!='INSPECTION_PROVISIONAL_PASS':check['finished_elapsed_s']=elapsed
                        break
            if kind=='RECOVERY_REQUIRED':
                self.data.setdefault('recoverable_faults',[]).append({'arm':arm,'reason':detail,'elapsed_s':elapsed})
                for check in self.data['inspection_attempts']:
                    if check['arm']==arm and check['outcome'] in ('in_progress','provisional'):
                        check.update(outcome='retry_required',finished_elapsed_s=elapsed)
            if kind in ('INSPECTION_GROUP_RESTARTED','JIG_RECHECK_AFTER_RESUME','PREPARATION_RECHECK_AFTER_RESUME','INSPECTION_RECHECK_STARTED'):
                for check in self.data['inspection_attempts']:
                    if check['arm']==arm and check['outcome'] in ('in_progress','provisional'):
                        check.update(outcome='restarted',finished_elapsed_s=elapsed)
            if kind in ('VISION_PREPARING','SAFE_POSE_STARTED','FOLLOWER_CONNECTING'):
                self.data['readiness_checks'].append({'arm':arm,'kind':{'VISION_PREPARING':'camera',
                    'SAFE_POSE_STARTED':'safe_pose','FOLLOWER_CONNECTING':'follower'}[kind],
                    'started_elapsed_s':elapsed,'finished_elapsed_s':None,'outcome':'in_progress'})
            if kind in ('VISION_READY','SAFE_POSE_REACHED','SAFE_POSE_SKIPPED','FOLLOWER_READY'):
                family={'VISION_READY':'camera','SAFE_POSE_REACHED':'safe_pose','SAFE_POSE_SKIPPED':'safe_pose','FOLLOWER_READY':'follower'}[kind]
                for check in reversed(self.data['readiness_checks']):
                    if check['arm']==arm and check['kind']==family:
                        if kind=='SAFE_POSE_SKIPPED':check['movement_skipped']=True
                        else:check.update(finished_elapsed_s=elapsed,outcome='ready')
                        break
            if kind=='STAGE_DONE':event['stage_code']=detail
            if kind=='ESTOP_REQUESTED':self.data['controls']['stop_requests']+=1
            if kind=='PAUSED' and not any(p['ended_elapsed_s'] is None for p in self.data['pause_intervals']):
                self.data['pause_intervals'].append({'started_elapsed_s':elapsed,'ended_elapsed_s':None,'ended_by':None})
            if kind in ('RESUMED','RESET_STARTED','DONE','CHAIN_DONE','RESET_DONE','FAILED','REJECTED'):
                for pause in self.data['pause_intervals']:
                    if pause['ended_elapsed_s'] is None:pause.update(ended_elapsed_s=elapsed,ended_by=kind)
            if kind=='RESUMED':self.data['controls']['resumes']+=1
            if kind=='RESET_STARTED':self.data['controls']['resets']+=1
            if kind in ('LINEAR_CHECK','LINEAR_STARTED','LINEAR_READY','LINEAR_DONE'):
                values={k:v for part in detail.split(':') if '=' in part for k,v in [part.split('=',1)]}
                try:target=float(values['target_mm'])
                except (KeyError,ValueError):target=None
                event['linear']={'target_mm':target,'position_measured':False,'completion_basis':'time_based'}
                if kind=='LINEAR_CHECK':
                    self.data['linear_requests'].append({'linear_request_id':len(self.data['linear_requests'])+1,
                        'arm':arm,'target_mm':target,'checked_elapsed_s':elapsed,'started_elapsed_s':None,
                        'finished_elapsed_s':None,'outcome':'checking','position_measured':False})
                else:
                    row=next((r for r in reversed(self.data['linear_requests'])
                        if r['target_mm']==target and r['outcome'] in ('checking','moving')),None)
                    if row is not None:
                        if kind=='LINEAR_STARTED':row.update(started_elapsed_s=elapsed,outcome='moving')
                        elif kind=='LINEAR_READY':row.update(finished_elapsed_s=elapsed,outcome='already_at_target',movement_seconds=0.)
                        else:row.update(finished_elapsed_s=elapsed,outcome='moved',
                            movement_seconds=elapsed-row['started_elapsed_s'] if row['started_elapsed_s'] is not None else None)
                        event['linear']['linear_request_id']=row['linear_request_id']
                if kind=='LINEAR_STARTED':self.data['linear_intervals'].append({'target_mm':target,'started_elapsed_s':elapsed,'ended_elapsed_s':None,'completion_basis':'time_based'})
                if kind=='LINEAR_DONE':
                    for interval in reversed(self.data['linear_intervals']):
                        if interval['ended_elapsed_s'] is None and interval['target_mm']==target:interval['ended_elapsed_s']=elapsed;break
            if kind in ('EPISODE_DONE','BUILD_DONE'):run['phase_completed']=True
            if kind=='PREPARATION_PROFILE':
                try:event['preparation_profile']=json.loads(detail)
                except ValueError:pass
            if kind=='FAILURE_IMAGES':self.data['artifacts']['failure_evidence']=detail
            terminal=None
            operation=self.data['operation'] or ''
            if kind=='CHAIN_DONE' or kind=='DONE' and not operation.startswith(('build_load_','pc_build_load_')):terminal='succeeded'
            if kind=='RESET_DONE':terminal='cancelled' if self.cancelled or self.data['category']=='production' else 'succeeded'
            if kind=='FAILED':terminal='interrupted' if (self.data['failure'] or {}).get('type') in ('ShutdownRequested','KeyboardInterrupt','SystemExit') else 'failed'
            if kind=='REJECTED':terminal='failed' if self.execution_started else 'rejected'
            if terminal:
                self.data.update(outcome=terminal,record_complete=True,finished_elapsed_s=elapsed,finished_at_unix=at)
                if terminal in ('failed','rejected') and self.data['failure'] is None:
                    self.data['failure']={'type':kind,'message':detail,'phase':None}
                for check in self.data['inspection_attempts']:
                    if check['outcome'] in ('in_progress','provisional'):
                        check.update(outcome='unresolved_at_end',finished_elapsed_s=elapsed)
                self.data['completed_without_intervention']=(terminal=='succeeded' and not any(self.data['controls'].values())
                    and not self.data.get('supervision_lost') and not self.data['pause_intervals']
                    and not self.data['control_requests'])
            return event

    def snapshot(self):
        with self.lock:
            # These values are copied on insertion and thereafter append-only or
            # replaced wholesale. Share their immutable entries with the writer;
            # copying camera/calibration/episode snapshots on every event is costly.
            frozen=('configurations','plans','jig_pose_sets','follower_observations','step_arrivals',
                    'inspection_by_arm','expected_inspections','preparation_profiles')
            result=deepcopy({k:v for k,v in self.data.items() if k not in frozen})
            result.update({k:self.data[k].copy() for k in frozen})
            return result
