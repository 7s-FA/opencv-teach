"""Persistent per-arm windows and controllers; selection never changes a running job."""
from copy import deepcopy
from pathlib import Path
import time
import queue
import tkinter as tk
from tkinter import ttk
from .arm_workspace import ARM_NAMES,arm_id,calibration_pending
from .domain import ROOT,load_profile,atomic_json,read_json


class ArmWorkspaces:
    def __init__(self,root,data_dir=None,*,render=True,auto_camera=True,auto_devices=True,mode_override=None,camera_only=False):
        from .ui import App
        from .configuration import ProfileLibrary
        self.root=root;self.data_dir=Path(data_dir or ROOT/'data');self.apps={};self.closing=False;self.finished=set();self.job=None;self.mode_override=mode_override
        self.linear_state_query=None;self.integration=None;self.pi_execution=None;self.camera_only=camera_only
        profile,_,_=load_profile(self.data_dir);self.active=arm_id(profile)
        library=ProfileLibrary(self.data_dir)
        profiles={arm_id(p):deepcopy(p) for p in library.items.values() if p.get('robot_id','arm2') in ARM_NAMES}
        profiles[self.active]=profile
        self.configured_modes={key:value.get('mode','follower') for key,value in profiles.items()}
        for key in (self.active,*(key for key in profiles if key!=self.active)):
            window=root if key==self.active else tk.Toplevel(root);window.withdraw()
            app=App(window,self.data_dir,render=render,auto_camera=auto_camera,auto_devices=False,
                    mode_override=mode_override,profile_override=profiles[key],camera_only=camera_only)
            app.workspace_manager=self;app.workspace_visible=key==self.active
            app.startup_devices_pending=auto_devices and not camera_only;self.apps[key]=app
            window.title(ARM_NAMES[key]+' · SO-101 Teach')
            app.arm_selector.grid_remove()
            controls=ttk.Frame(app.arm_selector.master);controls.grid(row=0,column=1,padx=(8,4))
            app.arm_quick_buttons={}
            for i,other in enumerate(profiles):
                button=ttk.Button(controls,text=ARM_NAMES[other],style='TButton',command=lambda k=other,a=app:a.guard(lambda:self.select(k)))
                button.grid(row=0,column=i,padx=2);app.arm_quick_buttons[other]=button
            app.connect_all_btn=ttk.Button(controls,text='전체 연결',style='TButton',command=lambda a=app:a.guard(self.connect_all))
            app.connect_all_btn.grid(row=0,column=len(profiles),padx=(8,2))
        first=self.apps[self.active]
        preferences_path=self.data_dir/'preferences.json'
        shared_preferences=read_json(preferences_path) if preferences_path.exists() else first.preferences
        for key in ('motion_speed','motion_speed_version'):shared_preferences[key]=first.preferences[key]
        shared_preferences['last_episode_id']=first.episode['id']
        atomic_json(preferences_path,shared_preferences)
        for app in self.apps.values():
            app.settings.library=first.settings.library
            app.preferences=shared_preferences
            app.catalog.items=first.catalog.items
            if (auto_devices or camera_only) and app.profile.get('mode')!='demo':
                app.guard(lambda app=app:app.episode_sync.refresh(preserve_local_changes=True))
                if not camera_only:
                    app.start_linear_state_query()
                    app.startup_job=app.root.after(200,app.startup_connections)
        if camera_only:first.startup_job=first.root.after(200,first.startup_camera_only)
        # Startup loads each arm's own last episode without changing the visible
        # selection or replacing either controller's state.
        self.refresh_buttons()

    def start_linear_state(self,app):
        from .linear_state import StateStream
        from .pi_connection import load
        source=(app.workcell_preview or {}).get('linear_stage',{}).get('state_source')
        if self.closing or not app.remote_mode or app.profile.get('mode')=='demo' or not source:return
        connection=load(app.data_dir);current=self.linear_state_query
        if current and not current.stop.is_set() and current.connection==connection and current.source==source:return
        if current:current.close()
        for target in self.apps.values():target.apply_linear_state({'known':False,'pending':True,'measured':False})
        self.linear_state_query=StateStream(connection,source);self.linear_state_query.start()

    def stop_linear_state(self):
        for app in self.apps.values():
            if not app.closed and app.remote_mode and app.profile.get('mode')!='demo':
                self.start_linear_state(app);return
        if self.linear_state_query:self.linear_state_query.close()
        for app in self.apps.values():app.apply_linear_state({'known':False,'connected':False,'measured':False})

    def poll_linear_state(self):
        if not self.linear_state_query or self.linear_state_query.stop.is_set():return
        try:state=self.linear_state_query.results.get_nowait()
        except queue.Empty:return
        for app in self.apps.values():app.apply_linear_state(state)

    def select(self,key):
        if key==self.active:return
        if self.closing or key not in self.apps:raise ValueError('선택할 로봇팔 작업창이 없습니다.')
        previous=self.apps[self.active];target=self.apps[key]
        if previous.settings.worker and previous.settings.worker.running or previous.settings.job:
            raise ValueError('보정 작업을 마친 뒤 로봇팔을 선택하세요.')
        if getattr(previous.session,'state',None)=='FOLLOW' or (previous.camera_task or {}).get('kind')=='follow':raise ValueError('리더 따라가기를 정지한 뒤 로봇팔을 선택하세요.')
        previous.live_adjust.stop(halt=False)
        self.save_profile(previous)
        # Neither stop_preview nor install_profile is called here: pending safe
        # entry, vision, planning, motion and their heartbeats remain arm-local.
        previous.workspace_visible=False;target.workspace_visible=True
        if target.page!=previous.page:target.show_page(previous.page,internal=True)
        if not previous.camera_needed():previous.suspend_camera()
        geometry=previous.root.geometry();target.root.geometry(geometry);target.root.update_idletasks()
        zoomed=False
        try:zoomed=bool(previous.root.attributes('-zoomed'))
        except tk.TclError:pass
        previous.root.withdraw();self.active=key
        target.view=deepcopy(previous.view);target.set_mode(previous.mode)
        target.last_render=None
        # Optional tabs (diagnostics) need not exist in the other workspace.
        selected=previous.settings.tabs.tab(previous.settings.tabs.select(),'text')
        matching=next((tab for tab in target.settings.tabs.tabs() if target.settings.tabs.tab(tab,'text')==selected),None)
        if matching is not None:target.settings.tabs.select(matching)
        target.teach_tabs.select(previous.teach_tabs.index('current'))
        target.root.deiconify()
        if zoomed:
            try:target.root.attributes('-zoomed',True)
            except tk.TclError:pass
        atomic_json(self.data_dir/'profile.json',self.persisted_profile(target))
        target.preferences['last_episode_id']=target.episode['id'];atomic_json(self.data_dir/'preferences.json',target.preferences)
        if target.camera_needed():
            if self.camera_only and (not target.remote or target.remote.error):target.startup_camera_only()
            else:target.guard(target.start_camera)
        target.notice(ARM_NAMES[key]+' 선택 · 다른 팔의 연결·토크·실행은 유지됩니다.')
        self.update_buttons()

    def connect_all(self):
        if self.closing:return
        if self.camera_only:raise ValueError('카메라 전용 실행에서는 모터를 연결하지 않습니다.')
        errors=[]
        for key,app in self.apps.items():
            if app.session and app.session.running:continue
            if calibration_pending(app.profile):errors.append(ARM_NAMES[key]+' 보정 필요');continue
            if app.settings.worker and app.settings.worker.running:errors.append(ARM_NAMES[key]+' 보정 중');continue
            if app.settings.pi_panel.job:continue
            try:
                if app.remote_mode:
                    app.startup_devices_pending=True;app.settings.pi_panel.connect()
                else:app.connect_devices()
            except Exception as exc:errors.append(ARM_NAMES[key]+': '+str(exc))
        self.apps[self.active].notice(' · '.join(errors) if errors else '전체 로봇팔 연결 중 · 기존 연결과 실행은 유지됩니다.',bool(errors))
        self.update_buttons()

    def live_workcell(self,app,*,safe=False):
        """Snapshot each controller's own calibrated sample, never the other editor's ticks."""
        placement=deepcopy(app.workcell_preview)
        if not placement:return placement
        key='arm3' if arm_id(app.profile)=='arm2' else 'arm2';other=self.apps.get(key)
        if safe or app.mode!='live':
            step=next((s for s in other.episode['steps'] if s.get('safe_boundary')=='start'),None) if other else None
            source=other
            if not step:
                source=self.apps.get(placement.get('safe_pose_fallback_arm',{}).get(key))
                if source:step=next((s for s in source.episode['steps'] if s.get('safe_boundary')=='start'),None)
            placement['other_arm_pose_kind']='safe' if step else 'reference'
            if step:
                placement['other_arm_joint_angles_rad']=list(source.reference.angles(step['ticks']))
                placement['other_arm_tcp']=deepcopy(other.profile['tcp'])
            return placement
        sample=other.live_preview_sample() if other else None
        valid=sample is not None
        placement.update(other_arm_dynamic=True,other_arm_visible=valid,other_arm_pose_kind='live' if valid else 'unavailable')
        if valid:
            placement['other_arm_joint_angles_rad']=list(other.reference.angles(sample.ticks))
            placement['other_arm_tcp']=deepcopy(other.profile['tcp'])
        return placement

    def persisted_profile(self,app):
        profile=deepcopy(app.profile)
        if self.mode_override:profile['mode']=self.configured_modes[arm_id(profile)]
        return profile

    def save_profile(self,app):
        profile=self.persisted_profile(app)
        app.settings.library.save(profile['name'],profile,app.settings.active_profile_key)

    def claim_camera(self,app):
        for other in self.apps.values():
            if other is app or not other.camera or not other.camera.running:continue
            if other.pending_execution or other.camera_task or getattr(getattr(other,'inspection_run',None),'phase',None)=='inspection':
                app.camera_status.set('다른 팔의 지그 측정 완료를 기다립니다.');return False
            other.suspend_camera()
            if other.camera.running:return False
        return True

    def require_shared_jig_idle(self,app):
        if any(other is not app and (other.jig_measurement_in_use() or other.settings.pi_panel.job) for other in self.apps.values()):
            raise ValueError('다른 팔의 측정·실행을 마친 뒤 공통 지그 설정을 바꾸세요.')

    def sync_catalog(self,source):
        for app in self.apps.values():
            if app is source:continue
            app.catalog.revision+=1
            app.roi=deepcopy(app.catalog.items.get(app.active_jig,{}).get('roi'))
            app.catalog_changed(_shared=True);app.settings.refresh_jigs()

    def update_buttons(self):
        connected=all(app.session and app.session.running for app in self.apps.values())
        pending=any(app.settings.pi_panel.job for app in self.apps.values())
        for app in self.apps.values():
            app.connect_all_btn.configure(text='모두 연결됨' if connected else '연결 중' if pending else '전체 연결')
            app.connect_all_btn.state(['disabled'] if self.camera_only or connected or pending else ['!disabled'])
            for key,button in app.arm_quick_buttons.items():
                target=self.apps[key];session=target.session
                state=getattr(session,'state',None) if session and session.running else None
                label={'READ_ONLY':'연결','HOLD':'유지','MOVING':'실행 중','ACTIVATING':'시작 중','FOLLOW':'따라가기','FAULT':'오류'}.get(state,'미연결')
                if target.pending_execution or target.safe_entry or target.remote_plan_job:label='실행 준비'
                text=f'{ARM_NAMES[key]} · {label}';style='Primary.TButton' if self.active==key else 'TButton'
                if button.cget('text')!=text:button.configure(text=text)
                if button.cget('style')!=style:button.configure(style=style)

    def refresh_buttons(self):
        if self.closing:return
        self.poll_linear_state();self.update_buttons();self.job=self.root.after(200,self.refresh_buttons)

    def close(self):
        if self.closing:return
        if getattr(getattr(self,'safe_shutdown',None),'busy',False):return
        live=any(app.profile.get('mode')!='demo' and (app.remote or app.session) for app in self.apps.values())
        if live and not self.camera_only:
            from .safe_shutdown import SafeShutdown
            self.safe_shutdown=SafeShutdown(self)
            try:self.safe_shutdown.start()
            except Exception as exc:self.safe_shutdown.fail(str(exc))
            return
        self.finish_safe_close()

    def finish_safe_close(self):
        if self.closing:return
        self.closing=True
        for app in self.apps.values():app.root.withdraw()
        if self.pi_execution and self.pi_execution.busy:self.pi_execution.cancel()
        if self.integration:self.integration.close()
        if self.linear_state_query:self.linear_state_query.close()
        if self.job:self.root.after_cancel(self.job)
        for app in self.apps.values():
            try:self.save_profile(app)
            except OSError as exc:app.notice('로봇 설정 저장 실패: '+str(exc),True)
        for app in self.apps.values():app.close()

    def finished_close(self,app):
        self.finished.add(arm_id(app.profile));app.root.withdraw()
        if len(self.finished)==len(self.apps):self.root.destroy()
