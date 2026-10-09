"""Open cameras for viewing or a bounded measurement, retaining only valid poses."""
import time
from copy import deepcopy

MEASUREMENT_MAX_AGE_SECONDS=3.0

class CameraLifecycle:
    @property
    def measurement_timeout_seconds(self):
        return self.pose_latch.stability.seconds*self.measurement_attempts_limit

    def measurement_within_attempts(self,result):
        return result.get('acquisition_completed_attempts',0)<=self.measurement_attempts_limit

    def measurement_failure(self,task,now,keys):
        keys=sorted(keys)
        obs=self.camera.observation if self.camera else None
        results=obs[1].get('by_jig',{self.active_jig:obs[1]}) if obs and obs[1] else {}
        failed=[key for key in keys if (r:=results.get(key,{})) and (not r.get('selected') or not self.measurement_within_attempts(r))
                and r.get('acquisition_completed_attempts',0)>=self.measurement_attempts_limit]
        if not failed and now-task.get('budget_started',task['started'])<self.measurement_timeout_seconds:return None
        missing=failed or [key for key in keys if not results.get(key,{}).get('selected')] or keys
        counts=[results.get(key,{}).get('stable_candidate_samples',0) for key in missing]
        reason=results[failed[0]].get('acquisition_issue') if failed else None
        if not reason:reason='관측 부족 (최소 2회 필요)' if not counts or min(counts)<2 else '유효한 최신 위치 미확인'
        catalog=getattr(getattr(self,'catalog',None),'items',{})
        names=' / '.join(catalog.get(key,{}).get('name',key) for key in missing)
        message=f'지그 측정 실패 · {names} · 최대 {self.measurement_attempts_limit}회 시도 · {reason}'
        # Preserve the failing attempt even after capture is stopped and its
        # image ages out. Diagnostics must never interfere with stopping a move.
        if getattr(self,'data_dir',None):
            from .domain import atomic_json
            fields=('status','acquisition_issue','acquisition_completed_attempts','stable_candidate_samples','adoption_inliers','pose_measured_at')
            report={'at':time.time(),'error':message,'action':task.get('action',task.get('kind')),
                    'started':task['started'],'observation_age_s':None if not obs else now-obs[2],
                    'observation_max_age_s':MEASUREMENT_MAX_AGE_SECONDS,'processing_seconds':obs[1].get('processing_seconds') if obs and obs[1] else None,
                    'required_jigs':{key:catalog.get(key,{}).get('name',key) for key in keys},
                    'results':{key:{**{field:results.get(key,{}).get(field) for field in fields},'selected':bool(results.get(key,{}).get('selected'))} for key in keys}}
            try:atomic_json(self.data_dir/'diagnostics'/f'jig-measurement-{time.time_ns()}.json',report)
            except OSError:pass
        return message

    def camera_needed(self):
        return bool(getattr(getattr(self,'inspection_run',None),'busy',False) or self.pending_execution or self.camera_task or
                    getattr(self,'workspace_visible',True) and self.page=='camera' and (self.camera_auto_view or self.camera_view_requested) and not self.camera_manual_off)

    def update_camera_mode(self):
        camera=self.camera
        if getattr(camera,'observer',False):return
        if not camera or not hasattr(camera,'set_mode'):return
        task=self.camera_task
        measuring=bool(self.pending_execution or task and task['kind'] in ('jig','follow') and not task.get('captured'))
        photo=bool(task and task['kind']=='photo')
        inspecting=bool(getattr(getattr(self,'inspection_run',None),'busy',False))
        processing=not inspecting and (measuring or not photo and not self.jig_updates_paused and not self.detector.frozen)
        camera.set_mode(processing_enabled=processing,preview_fps=10 if processing or photo or inspecting else 5)

    def camera_page_changed(self,previous):
        if self.page=='camera' and previous!='camera':
            self.camera_manual_off=False
            if self.camera_auto_view and self.profile.get('mode')!='demo':
                if not self.remote_mode or self.remote and not self.remote.error:self.guard(self.start_camera)
        elif previous=='camera' and self.page!='camera':
            self.camera_view_requested=False
            if not self.camera_needed():self.suspend_camera()

    def suspend_camera(self):
        """Release capture without erasing a confirmed pose or touching the motors."""
        self.camera_sleeping=True;self.camera_restart_pending=False;self.camera_waiting_for_arm=False
        if self.camera and not self.camera_close_requested:
            self.camera_close_requested=True;self.camera.close()
        if hasattr(self,'camera_status'):self.camera_status.set('카메라 꺼짐 · 필요할 때 연결합니다.')

    def stop_camera(self):
        self.camera_manual_off=True;self.camera_task=None
        if self.pending_execution:
            self.pending_execution=None;self.notice('카메라 해제 · 실행 전 지그 측정을 취소했습니다.')
        self.suspend_camera()

    def camera_results(self,now=None):
        if self.teaching_jig_hold_active():return deepcopy(self.teaching_jig_results)
        now=time.monotonic() if now is None else now
        if self.camera and (getattr(self.camera,'error',None) or getattr(self.camera,'recovering',False)):return {}
        obs=self.camera.observation if self.camera else None
        if not obs or not obs[1]:return {}
        _,detection,at=obs;results=detection.get('by_jig',{self.active_jig:detection})
        if not self.camera_sleeping:
            if not 0<=now-at<MEASUREMENT_MAX_AGE_SECONDS:return {}
            valid={}
            for key,result in results.items():
                measured=result.get('pose_measured_at',at)
                if result.get('selected'):
                    if measured is None or measured<getattr(self,'measurement_valid_after',0.) or measured>now:continue
                    if not result.get('pose_frozen') and now-measured>=self.pose_latch.seconds:continue
                valid[key]=result
            return valid
        # A deliberately stopped camera can keep its confirmed pose for the
        # configured hold time (or a running episode), never for a new read.
        if self.pending_execution or self.camera_task:return {}
        kept={}
        for key,result in results.items():
            measured=result.get('pose_measured_at',at)
            if measured is None or measured>now or measured<getattr(self,'measurement_valid_after',0.):continue
            remaining=max(0.,self.pose_latch.seconds-(now-measured))
            if result.get('selected') and (self.detector.frozen or remaining>0):
                kept[key]={**result,'pose_held':True,'pose_frozen':self.detector.frozen,'hold_remaining_s':remaining}
        return kept

    def camera_adoption_results(self,now=None):
        """Display accepted poses through their hold time, never for a new move."""
        now=time.monotonic() if now is None else now
        results=self.camera_results(now)
        if results or self.teaching_jig_hold_active():return results
        camera=self.camera
        if not camera or getattr(camera,'error',None) or getattr(camera,'recovering',False):return {}
        obs=camera.observation
        if not obs or not obs[1] or not 0<=now-obs[2]:return {}
        shown={}
        for key,result in obs[1].get('by_jig',{self.active_jig:obs[1]}).items():
            if not result.get('selected') and now-obs[2]<1.5 and 'stable_candidate_seconds' in result:
                shown[key]={**deepcopy(result),'display_only':True};continue
            measured=result.get('pose_measured_at')
            if not result.get('selected') or measured is None or not 0<=now-measured<self.pose_latch.seconds:continue
            if measured<getattr(self,'measurement_valid_after',0.):continue
            shown[key]={**deepcopy(result),'pose_held':True,'hold_remaining_s':self.pose_latch.seconds-(now-measured),'display_only':True}
        return shown

    def teaching_pause_results(self,now=None,*,since=None):
        """An explicit teaching pause fixes an unexpired accepted pose."""
        now=time.monotonic() if now is None else now
        camera=self.camera
        if not camera or getattr(camera,'error',None) or getattr(camera,'recovering',False):return {}
        obs=camera.observation
        if not obs or not obs[1] or obs[1].get('_preview') or not 0<=now-obs[2]:return {}
        if since is not None and obs[2]<since:return {}
        minimum=max(getattr(self,'measurement_valid_after',0.),since or 0.)
        accepted={}
        for key,result in obs[1].get('by_jig',{self.active_jig:obs[1]}).items():
            measured=result.get('pose_measured_at')
            if not (result.get('selected') or {}).get('metric') or measured is None:continue
            if measured<minimum or not 0<=now-measured<self.pose_latch.seconds:continue
            accepted[key]=deepcopy(result)
        return accepted

    def request_jig_read(self,*,keys=None,pause_all=False):
        if self.pending_execution or self.camera_task:raise ValueError('카메라 측정이 진행 중입니다.')
        if self.page=='teach' and self.teaching_jig_id()!=self.active_jig:self.select_camera_jig(self.teaching_jig_id())
        keys=list(keys) if keys is not None else [self.active_jig]
        if not keys or any(key not in self.catalog.items for key in keys):raise ValueError('측정할 지그를 확인하세요.')
        started=time.monotonic();self.refresh_jig_reference()
        self.camera_task={'kind':'jig','jig':keys[0],'jigs':keys,'pause_all':pause_all,'started':started,'budget_started':time.monotonic(),'page':self.page}
        try:self.start_camera();self.show_page('camera')
        except Exception:
            self.camera_task=None;self.suspend_camera()
            if pause_all:self.jig_updates_paused=False;self.teaching_jig_results.clear()
            raise
        self.notice('지그 다시 읽기 · 측정 후 이전 화면으로 돌아갑니다.')

    def request_follow_jig_read(self):
        """Follow uses the paused teaching basis, measuring once when missing."""
        if self.profile.get('mode')!='leader':raise ValueError('로봇 설정에서 리더 + 팔로워 모드를 선택하세요.')
        if self.jig_measurement_in_use():raise ValueError('진행 중인 측정·실행을 마친 뒤 리더 따라가기를 시작하세요.')
        key=self.teaching_jig_id()
        if key!=self.active_jig:self.select_camera_jig(key)
        held=self.jig_updates_paused and bool(self.held_jig_reference(key))
        started=time.monotonic()
        if not held:self.refresh_jig_reference()
        self.camera_task={'kind':'follow','jig':key,'started':started,'budget_started':time.monotonic(),'page':self.page,
                          'session':self.session,'episode':self.episode['id']}
        if held:
            self.camera_task['captured']=started
            self.notice('고정된 지그 위치로 리더 따라가기를 준비합니다.')
            self.update_camera_mode()
            if self.page!='camera':self.suspend_camera()
            return
        try:self.start_camera();self.show_page('camera',internal=True)
        except Exception:
            self.camera_task=None;self.suspend_camera();raise
        self.notice('지그를 먼저 탐지합니다. 확정 후 자동 갱신을 정지하고 리더 따라가기를 시작합니다.')

    def poll_follow_jig_task(self,task,now):
        try:
            if self.session is not task['session'] or not self.session.running or self.session.state!='HOLD':
                raise ValueError('팔로워 연결·상태 변경으로 리더 따라가기 준비를 취소했습니다.')
            if self.episode['id']!=task['episode'] or self.teaching_jig_id()!=task['jig']:
                raise ValueError('에피소드·지그 변경으로 리더 따라가기 준비를 취소했습니다.')
            if not task.get('captured'):
                obs=self.camera.observation if self.camera else None
                results=obs[1].get('by_jig',{self.active_jig:obs[1]}) if obs and obs[1] else {}
                result=results.get(task['jig'],{})
                measured=result.get('pose_measured_at')
                ready=bool(not getattr(self.camera,'error',None) and not getattr(self.camera,'recovering',False) and self.measurement_within_attempts(result) and obs and not self.camera_sleeping and 0<=now-obs[2]<MEASUREMENT_MAX_AGE_SECONDS and obs[2]>=task['started']
                           and (result.get('selected') or {}).get('metric') and measured is not None and measured>=task['started'])
                if not ready:
                    error=self.camera.error if self.camera else None
                    failure=self.measurement_failure(task,now,[task['jig']])
                    if error or failure:
                        raise ValueError(error or failure+' · 리더 따라가기를 시작하지 않았습니다.')
                    return
                self.remember_teaching_jigs({task['jig']:result},since=task['started'])
                self.jig_updates_paused=True;task['captured']=now
                self.update_camera_mode()
                self.update_jig_pause_button();self.update_jig_hint();self.last_render=None
                self.show_page(task['page'],internal=True)
                if self.page!='camera':self.suspend_camera()
            leader=self.leader_session
            if not leader or not leader.running:
                if not task.get('connecting'):
                    task['connecting']=True;self.prepare_leader_follow()
            elif leader.latest and leader.latest.fresh():
                if self.prepare_leader_follow():
                    # Consume the intent before dispatch so a poll cannot replay it.
                    self.camera_task=None
                    self.prepare_leader_assist()
                    try:self.session.request('follow')
                    except Exception:
                        self.stop_leader_assist();raise
                    self.taught_jig_display=None;self.preview_jig_references=None
                    self.measured_jig_display=self.held_teaching_scene_references();self.measured_target_active=True
                    self.notice('지그 확정 · 자동 갱신 정지 · 리더 따라가기 시작')
                    return
            if now-task['captured']>10:raise ValueError('리더 현재값을 받지 못해 따라가기를 시작하지 않았습니다.')
        except Exception as exc:
            self.camera_task=None;self.notice(str(exc),True)
            self.show_page(task['page'],internal=True)
            if self.page!='camera':self.suspend_camera()

    def request_camera_photo(self,done):
        if self.pending_execution or self.camera_task:raise ValueError('카메라 측정이 진행 중입니다.')
        self.camera_task={'kind':'photo','started':time.monotonic(),'page':self.page,'done':done}
        try:self.start_camera()
        except Exception:
            self.camera_task=None;self.suspend_camera();raise
        self.notice('카메라를 켜고 보정용 사진을 수집합니다.')

    def poll_camera_lifecycle(self):
        self.update_camera_mode()
        if self.camera_restart_pending:
            if not self.camera_needed():self.camera_restart_pending=False
            elif not self.camera or not self.camera.running:
                self.camera_restart_pending=False;self.guard(self.start_camera)
        if getattr(self,'camera_waiting_for_arm',False):return
        task=self.camera_task
        if not task:return
        if task['kind']=='follow':
            self.poll_follow_jig_task(task,time.monotonic());return
        keys=task.get('jigs',[task.get('jig')]) if task['kind']!='photo' else []
        obs=self.camera.observation if self.camera else None;now=time.monotonic();ready=False
        if task['kind']=='photo' and self.camera:
            preview=getattr(self.camera,'preview_frame',None)
            if preview:obs=(preview[0],None,preview[1])
        pause_results=None
        if task['kind']=='jig' and self.jig_updates_paused and not self.camera_sleeping:
            pause_results=self.teaching_pause_results(now,since=task['started'])
            ready=all(pause_results.get(key,{}).get('selected') and self.measurement_within_attempts(pause_results[key]) for key in keys)
        elif obs and not self.camera_sleeping and 0<=now-obs[2]<(1. if task['kind']=='photo' else MEASUREMENT_MAX_AGE_SECONDS) and obs[2]>=task['started']:
            if task['kind']=='photo':ready=True
            else:
                results=obs[1].get('by_jig',{self.active_jig:obs[1]}) if obs[1] else {}
                ready=all(self.measurement_within_attempts(results.get(key,{})) and results.get(key,{}).get('selected') and results[key].get('pose_measured_at') is not None and results[key]['pose_measured_at']>=task['started'] for key in keys)
        error=self.camera.error if self.camera else None
        if error or getattr(self.camera,'recovering',False):ready=False
        failure=None if ready else self.measurement_failure(task,now,keys) if task['kind']!='photo' else ('사진 수신 시간 초과' if now-task['started']>=10 else None)
        if not ready and not error and not failure:return
        self.camera_task=None
        try:
            if not ready:raise ValueError(error or failure)
            if task['kind']=='photo':task['done'](obs[0].copy())
            else:
                if self.jig_updates_paused:
                    results=pause_results if pause_results is not None else obs[1].get('by_jig',{self.active_jig:obs[1]})
                    self.remember_teaching_jigs({key:results.get(key,{}) for key in keys},since=task['started'])
                    self.notice(f'전체 지그 {len(keys)}개 채택 완료 · 자동 갱신 정지' if task.get('pause_all') else '지그 다시 읽기 완료 · 자동 갱신 정지 상태로 위치를 유지합니다.')
                else:self.notice('지그 위치 읽기 완료 · 설정한 위치 유지 시간 동안 사용합니다.')
        except Exception as exc:
            if task.get('pause_all'):
                self.jig_updates_paused=False;self.teaching_jig_results.clear()
                self.notice(str(exc)+' · 전체 지그 채택 미완료 · 자동 갱신을 계속합니다.',True)
            else:self.notice(str(exc),True)
        finally:
            self.update_camera_mode()
            self.show_page(task['page'])
            if self.page!='camera':self.suspend_camera()

    def jig_measurement_in_use(self):
        return bool(getattr(getattr(self,'inspection_run',None),'busy',False) or self.pending_execution or self.camera_task or self.safe_entry or self.remote_plan_job or
                    self.playing or self.detector.frozen or
                    getattr(self.session,'program_active',None) and self.session.program_active.is_set())

    def teaching_jig_hold_active(self):
        held_move=(getattr(self,'move_uses_held_jig',False) and not self.pending_execution and not self.camera_task and not self.safe_entry)
        return self.jig_updates_paused and (held_move or not self.jig_measurement_in_use())

    def remember_teaching_jigs(self,results,*,since=None):
        for key,result in results.items():
            if not result or not result.get('selected',{}):continue
            if not result['selected'].get('metric') or key not in self.catalog.items:continue
            measured=result.get('pose_measured_at')
            if since is not None and (measured is None or measured<since):continue
            self.teaching_jig_results[key]={**deepcopy(result),'teaching_held':True,'pose_held':True,
                                           'pose_frozen':False,'hold_remaining_s':0.}

    def update_jig_pause_button(self):
        text='전체 지그 채택 중' if (self.camera_task or {}).get('pause_all') else '● 갱신 정지 중 · 재개' if self.jig_updates_paused else '자동 갱신 정지'
        style='Primary.TButton' if self.jig_updates_paused else 'TButton'
        if self.jig_pause_btn.cget('text')!=text or self.jig_pause_btn.cget('style')!=style:
            self.jig_pause_btn.configure(text=text,style=style)
        self.jig_pause_btn.state(['disabled'] if self.jig_measurement_in_use() else ['!disabled'])

    def pause_jig_updates_for_teaching(self):
        """Idempotent pause before follow; never resume or invent a missing pose."""
        if self.jig_measurement_in_use():raise ValueError('지그 측정·경로 실행이 끝난 뒤 리더 따라가기를 시작하세요.')
        if not self.jig_updates_paused:
            results=self.teaching_pause_results();self.teaching_jig_results.clear()
            self.remember_teaching_jigs(results,since=max(getattr(self,'measurement_valid_after',0.),getattr(self,'teaching_resume_at',0.)))
            self.jig_updates_paused=True
        self.update_camera_mode();self.update_jig_pause_button();self.update_jig_hint();self.last_render=None

    def toggle_jig_updates(self):
        if self.jig_measurement_in_use():raise ValueError('측정·실행이 끝난 뒤 자동 갱신을 변경하세요.')
        if self.jig_updates_paused:
            self.refresh_jig_reference()
            self.jig_updates_paused=False;self.teaching_jig_results.clear();self.teaching_resume_at=time.monotonic()
            self.notice('지그 자동 갱신 재개 · 카메라 사용 중 새 측정값을 반영합니다.')
        else:
            results=self.teaching_pause_results();self.teaching_jig_results.clear()
            self.remember_teaching_jigs(results,since=max(getattr(self,'measurement_valid_after',0.),getattr(self,'teaching_resume_at',0.)))
            self.jig_updates_paused=True
            key=self.teaching_jig_id() if self.page=='teach' else self.active_jig
            keys=list(self.catalog.items) if self.page=='camera' else [key]
            missing=[key for key in keys if key not in self.teaching_jig_results]
            if missing:
                if self.page=='camera':
                    self.request_jig_read(keys=keys,pause_all=True)
                    names=' / '.join(self.catalog.items[key]['name'] for key in missing)
                    self.notice('전체 지그 채택 대기 · '+names+' · 모두 확정된 뒤 갱신을 정지합니다.')
                else:
                    self.request_jig_read()
                    self.notice('현재 확정값이 없어 한 번 측정한 뒤 자동 갱신을 정지합니다.')
            else:self.notice('지그 위치 확정 · 새 스텝은 이 위치를 기준으로 저장합니다. 기존 스텝은 유지됩니다.')
        self.update_camera_mode();self.update_jig_pause_button();self.update_jig_hint();self.last_render=None
