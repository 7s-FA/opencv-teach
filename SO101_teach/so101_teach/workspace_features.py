"""Workspace orchestration without device writes outside explicit session actions."""
from copy import deepcopy
import time,uuid,re
from .domain import ROOT,JOINTS,LABELS,load_profile,EpisodeStore,atomic_json
from .geometry import Kinematics
from .motion import MotionSession
from .playback import Playback
class WorkspaceFeatures:
    def set_pose_frozen(self,enabled):
        self.detector.freeze(enabled);self.update_camera_mode()
    def selected_jig_id(self):
        i=self.step_jig_choice.current() if hasattr(self,'step_jig_choice') else 0;keys=list(self.catalog.items)
        return keys[max(0,min(i,len(keys)-1))]
    def set_step_jig(self,key):
        keys=list(self.catalog.items)
        if key in keys:self.step_jig_choice.current(keys.index(key))
    def select_camera_jig(self,key=None):
        keys=list(self.catalog.items);user_selection=key is None
        if user_selection and self.camera_jig_choice.current()==0:
            self.camera_all.set(True);self.change_camera_view();return
        key=key or keys[max(0,self.camera_jig_choice.current()-1)]
        if self.detector.frozen:
            if user_selection and key==self.active_jig:
                self.camera_all.set(False);self.change_camera_view();return
            self.camera_jig_choice.current(0 if self.camera_all.get() else keys.index(self.active_jig)+1)
            raise ValueError('실행 중에는 감지할 지그를 바꾸지 않습니다.')
        self.active_jig=key;self.detector.active=key;self.detector.refresh();self.pose_latch=self.detector.latches[key];self.mesh_profile=self.catalog.mesh(key);self.roi=self.catalog.items[key].get('roi')
        self.camera_jig_choice.current(0 if self.camera_all.get() else keys.index(key)+1)
        # Never put the new jig's ROI on an image annotated for the previous jig.
        self.cancel_roi_points();self.camera_display_snapshot=None;self.camera_canvas.delete('image')
        self.camera_status.set(f'{self.catalog.items[key]["name"]} · 최신 감지 결과 확인 중')
        self.draw_camera_frame(time.monotonic())
        if user_selection:self.camera_all.set(False);self.change_camera_view()
        else:self.update_camera_profile_text()
    def catalog_changed(self,*,_shared=False):
        self.close_second_editor()
        self.teaching_jig_results.clear()
        old=self.selected_jig_id();keys=list(self.catalog.items);names=[self.catalog.items[k]['name'] for k in keys]
        self.step_jig_choice.configure(values=names);self.step_jig_choice.current(keys.index(old) if old in keys else 0)
        self.camera_jig_choice.configure(values=['전체 지그',*names]);self.select_camera_jig(self.active_jig if self.active_jig in keys else keys[0]);self.detector.clear();self.display_poses.clear();self.update_jig_hint()
        if self.workspace_manager and not _shared:self.workspace_manager.sync_catalog(self)
    def persist_configuration(self):
        atomic_json(self.data_dir/'profile.json',self.profile);self.detector.profile=self.profile
    def update_kinematics(self):
        self.kin=Kinematics(self.reference,tcp=self.profile['tcp']);self.last_fk_angles=None;self.last_render=None;self.render_context+=1
    def invalidate_jig_measurements(self):
        self.teaching_jig_results.clear()
        self.measurement_valid_after=time.monotonic();self.detector.clear();self.display_poses.clear();self.last_render=None
    def install_profile(self,profile):
        from .arm_workspace import arm_id,calibration_pending
        if self.workspace_manager and arm_id(profile)!=arm_id(self.profile):
            self.workspace_manager.select(arm_id(profile));return
        if self.session and self.session.running or self.leader_session and self.leader_session.running:raise ValueError('로봇 연결을 해제한 뒤 적용하세요.')
        if self.episode['steps'] and not calibration_pending(self.profile):self.save_episode()
        previous_profile=deepcopy(self.profile);previous_angles=self.reference.angles(self.target)
        from .configuration import model_tcp
        if profile.get('tcp',{}).get('mode','model')=='model':profile['tcp']=model_tcp(point=profile.get('tcp',{}).get('model_point','tip'))
        self.end_safe_edit(restore=False)
        self.close_second_editor()
        previous_sha=self.calibration.sha256;previous_trims=self.reference.trims.copy();previous_camera=deepcopy(self.profile['camera']);previous_mode=self.profile.get('mode');previous_target=self.target.copy()
        from .domain import Calibration,ModelReference
        candidate_cal=Calibration(self.data_dir/profile['calibration_file'])
        if candidate_cal.angle_mapping:
            profile=deepcopy(profile)
            profile['model_reference']['trim_ticks']=dict.fromkeys(JOINTS,0)
        candidate_ref=ModelReference(candidate_cal,profile['model_reference']);Kinematics(candidate_ref,tcp=profile['tcp'])
        # The same saved absolute work-surface plane applies on restart and
        # profile selection. Do not reinterpret legacy values as a new offset.
        if self.adjustments_path.exists():
            from .domain import read_json
            saved=read_json(self.adjustments_path)
            if saved.get('calibration_sha256')==candidate_cal.sha256:
                from .height_reference import restore_model_floor
                profile=restore_model_floor(profile,saved)
                candidate_ref.set_trims(dict.fromkeys(JOINTS,0) if candidate_cal.angle_mapping else profile['model_reference'].get('trim_ticks',saved['trim_ticks']))
        atomic_json(self.data_dir/'profile.json',profile);self.profile=deepcopy(profile);
        from .height_reference import floor_adjustment
        self.table_z.set(f'{floor_adjustment(profile):g}');self.catalog.floor_profile=deepcopy(profile);
        self.calibration=candidate_cal;self.reference=candidate_ref
        same=previous_sha==self.calibration.sha256 and arm_id(previous_profile)==arm_id(profile)
        if same and not self.calibration.angle_mapping and 'trim_ticks' not in profile['model_reference']:self.reference.set_trims(previous_trims)
        self.session=None;self.leader_session=None;self.latest=None;self.connection.set('모터 미연결')
        for n,label in zip(JOINTS,LABELS):self.current_vars[n].set(label+' · 현재 —')
        self.detector.profile=self.profile;self.update_kinematics();self.store=EpisodeStore(self.data_dir/'episodes',self.calibration,arm_id(profile))
        if not same:
            self.episode=self.store.new();self.selected=None;self.episode_name.set(self.episode['name']);self.follow_jig.set(False)
        self.apply_target(previous_target if same else self.reference.middle)
        for n,m in self.calibration.motors.items():
            self.sliders[n].configure(from_=m.low,to=m.high)
            if hasattr(self,'spins'):self.spins[n].configure(from_=m.low,to=m.high)
            if hasattr(self,'trim_vars'):self.trim_vars[n].set(self.reference.trims[n])
        self.update_angle_controls()
        self.refresh_library();self.refresh_steps();self.invalidate_jig_measurements()
        for w in self.pages['devices'].winfo_children():w.destroy()
        self.build_devices(self.pages['devices'])
        if hasattr(self,'settings'):self.settings.refresh_current_settings()
        if hasattr(self,'update_selected_arm_preview'):self.update_selected_arm_preview(previous_profile,previous_angles)
        if self.camera and (previous_camera!=self.profile['camera'] or previous_mode!=self.profile.get('mode')):
            self.suspend_camera();self.camera_restart_pending=self.camera_needed() and self.profile.get('mode')!='demo'
    def open_help(self):self.show_page('settings');self.settings.tabs.select(self.settings.pages['help'])
    def install_teaching_shortcuts(self,widget):
        # Before Tk class bindings: a focused checkbutton must not toggle first.
        tag='TeachShortcuts'+str(id(self))
        if not getattr(self,'_teaching_shortcuts_bound',False):
            self.root.bind_class(tag,'<KeyPress-space>',self.space_capture)
            self.root.bind_class(tag,'<KeyRelease-space>',self.space_key_release)
            for key in ('z','Z'):
                self.root.bind_class(tag,f'<KeyPress-{key}>',self.jig_key_press)
                self.root.bind_class(tag,f'<KeyRelease-{key}>',self.jig_key_release)
            self._teaching_shortcuts_bound=True
        tags=widget.bindtags()
        if tag not in tags:widget.bindtags((tag,*tags))
        for child in widget.winfo_children():self.install_teaching_shortcuts(child)
    def teaching_text_input(self,widget):
        kind=widget.winfo_class()
        if kind=='TCombobox':return str(widget.cget('state'))!='readonly'
        return kind in ('Entry','TEntry','Spinbox','TSpinbox','Text')
    def teaching_pointer_focus(self,event):
        if self.page!='teach' or self.episode_adjusting():
            if event.widget.winfo_class() in ('Frame','TFrame','Label','TLabel','Canvas','Button','TButton','Checkbutton','TCheckbutton','Radiobutton','TRadiobutton'):
                event.widget.master.focus_set()
            return
        # Clicking a background should leave text entry; selections themselves
        # stay unchanged. Numeric FocusOut continues to commit edited ticks.
        if event.widget.winfo_class() in ('Frame','TFrame','Label','TLabel','Canvas','Button','TButton','Checkbutton','TCheckbutton','Radiobutton','TRadiobutton'):
            editor=self.editor_for_widget(event.widget)
            if editor is self and self.preview_editor==2 and self.second_editor:editor=self.second_editor
            editor.editor_card.focus_set()
        elif not self.teaching_text_input(event.widget) and event.widget.winfo_class()!='TCombobox':
            event.widget.focus_set()
    def finish_teaching_title(self,event):
        if event.widget.winfo_class() in ('Entry','TEntry'):
            self.finish_teaching_selection(event)
            return 'break'
    def finish_teaching_selection(self,event):
        if self.page=='teach' and not self.episode_adjusting():self.editor_for_widget(event.widget).editor_card.focus_set()
        else:event.widget.master.focus_set()
    def space_capture(self,event):
        if getattr(event,'state',0)&(1|4|8|64):return
        if self.safe_editing or self.page!='teach' or self.episode_adjusting() or self.teaching_text_input(event.widget):return
        editor=self.editor_for_widget(event.widget)
        if getattr(self,'_space_release_job',None):
            self.root.after_cancel(self._space_release_job);self._space_release_job=None
        if getattr(self,'_space_down',False):return 'break'
        self._space_down=True
        now=time.monotonic()
        if now<getattr(self,'_space_next_at',0.):return 'break'
        self._space_next_at=now+1.
        self.guard(self.editor_for_widget(event.widget).capture);return 'break'
    def cancel_space_key(self,event=None):
        if getattr(self,'_space_release_job',None):self.root.after_cancel(self._space_release_job)
        self._space_release_job=None;self._space_down=False
    def space_key_release(self,event):
        if not getattr(self,'_space_down',False):return
        if getattr(self,'_space_release_job',None):self.root.after_cancel(self._space_release_job)
        # X11 repeat generates a release immediately followed by another press.
        self._space_release_job=self.root.after(35,self.cancel_space_key)
        return 'break'
    def jig_shortcut_allowed(self,widget):
        return (self.page=='teach' and not self.episode_adjusting() and not self.safe_editing and
                not self.teaching_text_input(widget) and
                'disabled' not in self.editor_for_widget(widget).follow_jig_check.state())
    def cancel_jig_key(self,event=None):
        if getattr(self,'_jig_key_release_job',None):self.root.after_cancel(self._jig_key_release_job)
        self._jig_key_release_job=None;self._jig_key_down=False
    def jig_key_press(self,event):
        if not self.jig_shortcut_allowed(event.widget) or event.state&(1|4|8|64|128):return
        if getattr(self,'_jig_key_release_job',None):
            self.root.after_cancel(self._jig_key_release_job);self._jig_key_release_job=None
        if getattr(self,'_jig_key_down',False):return 'break'
        self._jig_key_down=True
        editor=self.editor_for_widget(event.widget)
        editor.follow_jig_check.invoke()
        self.notice('지그 따라가기 '+('ON · 다음 저장 스텝에 적용' if editor.follow_jig.get() else 'OFF · 다음 저장 스텝은 고정 틱'))
        return 'break'
    def jig_key_release(self,event):
        if not getattr(self,'_jig_key_down',False):return
        if getattr(self,'_jig_key_release_job',None):self.root.after_cancel(self._jig_key_release_job)
        # X11 auto-repeat can deliver adjacent release/press pairs.
        self._jig_key_release_job=self.root.after(35,self.cancel_jig_key)
        return 'break'
    def save_as_episode(self):
        self.close_second_editor()
        self.store.validate(self.episode);doc=deepcopy(self.episode);doc.update(id=uuid.uuid4().hex,name=self.episode_name.get().strip()+' 복사',created_at=time.time())
        self.store.save(doc);self.sync_remote_episode(doc);self.episode=doc;self.episode_name.set(doc['name']);self.refresh_library();self.notice('새 에피소드로 복사했습니다. 이름을 편집할 수 있습니다.')
    def episode_delete_busy(self):
        return bool(getattr(self,'remote_plan_job',None) or self.pending_execution or self.camera_task or self.safe_entry or
                    getattr(self.session,'state','') in ('ACTIVATING','MOVING','FOLLOW') or
                    getattr(self.session,'program_active',None) and self.session.program_active.is_set() or
                    getattr(self.session,'command_pending',None) and self.session.command_pending.is_set())
    def cancel_episode_delete(self):
        panel=getattr(self,'episode_delete_panel',None)
        if panel is not None:panel.destroy()
        self.episode_delete_panel=None;self.episode_delete_target=None
    def request_episode_delete(self):
        if self.episode_delete_busy():raise ValueError('측정·실물 실행이 끝난 뒤 에피소드를 삭제하세요.')
        entry=next(((path,doc) for path,doc in self.library_entries if doc['id']==self.episode['id']),None)
        if entry is None:raise ValueError('삭제할 저장된 에피소드를 선택하세요.')
        self.stop_preview(quiet=True);self.cancel_episode_delete();self.episode_delete_target=entry[1]['id']
        from tkinter import ttk
        panel=self.episode_delete_panel=ttk.Frame(self.pages['teach'],style='Card.TFrame',padding=20,relief='solid',borderwidth=1)
        panel.place(relx=.5,rely=.35,anchor='center');panel.lift()
        ttk.Label(panel,text='에피소드 삭제',style='Section.TLabel').pack(anchor='w')
        ttk.Label(panel,text='“'+entry[1]['name']+'”\n이 에피소드와 포함된 스텝을 모두 삭제합니다. 되돌릴 수 없습니다.',wraplength=420,justify='left').pack(anchor='w',pady=12)
        row=ttk.Frame(panel,style='Card.TFrame');row.pack(fill='x')
        self.button(row,'취소',self.cancel_episode_delete).pack(side='left')
        confirm=self.button(row,'에피소드 삭제',self.confirm_episode_delete);confirm.configure(style='Stop.TButton');confirm.pack(side='right')
    def confirm_episode_delete(self):
        episode_id=getattr(self,'episode_delete_target',None)
        if episode_id is None:return
        if episode_id!=self.episode['id']:
            self.cancel_episode_delete();raise ValueError('선택한 에피소드가 바뀌었습니다. 삭제할 이름을 다시 확인하세요.')
        if self.episode_delete_busy():raise ValueError('측정·실물 실행이 끝난 뒤 삭제하세요.')
        ids=[d['id'] for _,d in self.library_entries];index=ids.index(episode_id)
        if self.remote_mode:self.require_remote().rpc('delete_episode',{'id':episode_id})
        self.store.delete(episode_id)
        self.cancel_episode_delete();self.stop_preview(quiet=True);self.end_safe_edit(restore=False)
        remaining=self.store.entries();self.episode=remaining[min(index,len(remaining)-1)][1] if remaining else self.store.new()
        self.selected=None;self.episode_name.set(self.episode['name']);self.follow_jig.set(False);self.step_name.set('자세 1')
        self.transport=Playback();self.last_plan=[];self.play_targets=[];self.timeline.configure(to=1);self.transport_label.set('0.0 / 0.0초')
        self.apply_target(self.reference.middle);self.set_mode('target');self.update_jig_hint();self.refresh_library();self.refresh_steps()
        if self.episode['steps']:self.steps.selection_set(self.episode['steps'][0]['id']);self.select_step()
        if remaining:self.preferences['last_episode_id']=self.episode['id']
        else:self.preferences.pop('last_episode_id',None)
        self.save_preferences();self.notice('에피소드와 포함된 스텝을 삭제했습니다.')
    def begin_safe_edit(self):
        self.live_adjust.stop()
        if self.safe_editing:self.end_safe_edit();return
        self.stop_preview(quiet=True)
        self.safe_draft={'ticks':self.target.copy(),'name':self.step_name.get(),'follow':self.follow_jig.get(),'jig':self.selected_jig_id()}
        self.safe_editing=True
        saved=next((s for s in self.episode['steps'] if s.get('safe_boundary')=='start'),None)
        if saved:self.apply_target(saved['ticks'])
        self.edit_title.set('안전 자세 설정');self.edit_hint.set('실물값·슬라이더로 맞춘 뒤 적용하세요.')
        self.step_name.set('안전 자세');self.step_name_entry.state(['readonly']);self.follow_jig.set(False)
        self.follow_jig_check.state(['disabled']);self.step_jig_choice.configure(state='disabled');self.update_jig_hint()
        self.add_step_btn.configure(text='안전 자세 적용');self.safe_edit_btn.configure(text='안전 설정 닫기',style='Primary.TButton')
        self.update_step_btn.state(['disabled']);self.capture_btn.state(['disabled']);self.set_mode('target')
        self.notice('시작·끝에서 사용할 고정 자세를 설정합니다. 적용만으로 로봇이 움직이지 않습니다.')
    def end_safe_edit(self,restore=True):
        if not self.safe_editing:return
        draft=self.safe_draft;self.safe_editing=False;self.safe_draft=None
        self.edit_title.set('스텝 자세 편집');self.edit_hint.set('슬라이더 편집 → 새로 추가 또는 수정')
        self.step_name_entry.state(['!readonly']);self.follow_jig_check.state(['!disabled']);self.step_jig_choice.configure(state='readonly')
        self.add_step_btn.configure(text='새 스텝 추가');self.safe_edit_btn.configure(text='안전 자세 설정',style='TButton')
        if restore and draft:
            self.apply_target(draft['ticks']);self.step_name.set(draft['name']);self.follow_jig.set(draft['follow']);self.set_step_jig(draft['jig'])
        self.update_jig_hint()
    def add_safe_steps(self):
        ticks=self.calibration.ticks({n:int(v.get()) for n,v in self.tick_vars.items()})
        middle=[s for s in self.episode['steps'] if not s.get('safe_boundary')]
        start=self.store.step(ticks,'시작 · 안전 자세');start['safe_boundary']='start'
        end=self.store.step(ticks,'종료 · 안전 자세');end['safe_boundary']='end'
        self.episode['steps']=[start,*middle,end];self.selected=start['id'];self.follow_jig.set(False);self.refresh_steps();self.save_episode();self.notice('현재 스텝 자세를 시작·종료 고정 안전 자세로 저장했습니다.')
    def append_step(self,step):
        steps=self.episode['steps']
        previous=next((s for s in reversed(steps) if not s.get('safe_boundary')),None)
        base=lambda name:re.sub(r' \(\d+\)$','',name)
        if (previous and base(previous['name'])==base(step['name'])
                and all(previous.get(key)==step.get(key) for key in ('ticks','jig_id','jig_reference'))):
            self.notice('직전 스텝과 이름·관절값·지그 설정이 같아 추가하지 않았습니다.')
            return False
        names={s['name'] for s in steps}
        if step['name'] in names:
            stem=base(step['name']);number=1
            while f'{stem} ({number})' in names:number+=1
            step['name']=f'{stem} ({number})'
        if self.episode['steps'] and self.episode['steps'][-1].get('safe_boundary')=='end':self.episode['steps'].insert(len(self.episode['steps'])-1,step)
        else:self.episode['steps'].append(step)
        return True
    def pause_preview(self):
        if self.pending_execution or self.safe_entry or getattr(self.session,'state','') in ('MOVING','FOLLOW','ACTIVATING'):return
        if not self.transport.targets:return
        self.playing=True;self.transport.paused=not self.transport.paused;self.transport.last=time.monotonic();self.set_pose_frozen(True);self.pause_btn.configure(text='이어보기' if self.transport.paused else '일시정지')
    def seek_preview(self,value):
        if self.pending_execution or self.safe_entry or getattr(self.session,'state','') in ('MOVING','FOLLOW','ACTIVATING'):return
        if getattr(self,'transport_sync',False) or not self.transport.targets:return
        was_playing=self.playing;self.transport.seek(float(value))
        if not was_playing:self.transport.paused=True;self.playing=True;self.pause_btn.configure(text='이어보기');self.set_pose_frozen(True)
        ticks,_=self.transport.sample();self.apply_target(ticks,preview=True);self.set_mode('target')
    def poll_extended(self):
        if self.leader_session and self.leader_session.error!=getattr(self,'last_leader_error',None):
            self.last_leader_error=self.leader_session.error
            if self.last_leader_error:self.device_notice('리더 연결: '+self.last_leader_error,True)
        self.poll_camera_lifecycle()
        if self.safe_entry and self.session:
            state=self.session.state
            if not self.session.running or state=='FAULT':self.safe_entry=None
            elif state=='HOLD' and not self.session.program_active.is_set():
                pending=self.safe_entry;self.safe_entry=None
                # Only a completed entry, never a user pause or failed arrival, advances.
                if self.session.completed_request_id==pending['request_id']:
                    if pending.get('current') is not None:self.begin_execution('play',pending['steps'],pending['current'])
                    else:self.prepare_execution('play',pending['steps'])
                else:self.notice('시작 안전 자세 이동 미완료 · '+(getattr(self,'last_motion_stop',None) or '다음 동작을 취소했습니다.'))
