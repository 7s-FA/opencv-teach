"""Switch one active arm in the existing teaching workspace."""
from copy import deepcopy
import time
import tkinter as tk
from tkinter import ttk
from .arm_workspace import ARM_NAMES,arm_id,calibration_pending,scene_placement
from .domain import read_json


class ArmSelectionControls:
    def build_arm_selector(self,header):
        self.arm_switch_job=None;self.arm_choice=tk.StringVar()
        self.arm_selector=ttk.Combobox(header,textvariable=self.arm_choice,state='readonly',width=19)
        self.arm_selector.grid(row=0,column=1,padx=(8,4));self.arm_selector.bind('<<ComboboxSelected>>',lambda e:self.guard(self.select_arm))
        self.refresh_arm_selector()

    def refresh_arm_selector(self):
        if not hasattr(self,'arm_selector'):return
        path=self.data_dir/'robot_profiles.json'
        library=self.settings.library.items if hasattr(self,'settings') else read_json(path) if path.exists() else {'default':self.profile}
        self.arm_profile_keys=[];labels=[]
        for key,profile in library.items():
            if profile.get('robot_id','arm2') not in ARM_NAMES:continue
            self.arm_profile_keys.append(key)
            labels.append(ARM_NAMES[arm_id(profile)]+(' · 보정 필요' if calibration_pending(profile) else ''))
        self.arm_selector.configure(values=labels)
        self.arm_choice.set(ARM_NAMES[arm_id(self.profile)]+(' · 보정 필요' if calibration_pending(self.profile) else ''))
        if hasattr(self,'connect_btn') and not (self.session and self.session.running):
            self.connect_btn.configure(text='팔3 보정' if calibration_pending(self.profile) else '팔로워 연결')

    def select_arm(self):
        index=self.arm_selector.current()
        if index<0:return
        key=self.arm_profile_keys[index];candidate=deepcopy(self.settings.library.items[key])
        if self.workspace_manager:
            self.workspace_manager.select(arm_id(candidate));return
        try:
            if self.arm_switch_job:raise ValueError('로봇팔 전환이 진행 중입니다.')
            if arm_id(candidate)==arm_id(self.profile):return
            if self.settings.worker and self.settings.worker.running or self.playing or self.pending_execution or self.remote_plan_job or self.camera_task:
                raise ValueError('실행·측정·보정이 끝난 뒤 로봇팔을 바꾸세요.')
            if self.settings.pi_panel.job:raise ValueError('Pi 연결이 끝난 뒤 로봇팔을 바꾸세요.')
            if self.remote and self.remote.error:raise ValueError('Pi 통신 복구 후 로봇팔을 바꾸세요.')
            if getattr(self.leader_session,'assist_state','OFF') not in ('OFF','FAULT'):raise ValueError('리더 보조를 해제한 뒤 로봇팔을 바꾸세요.')
            sessions=[s for s in (self.session,self.leader_session) if s and s.running]
            if self.session and self.session.running:
                sample=self.latest
                if self.session.state!='READ_ONLY' or not sample or not sample.fresh() or len(sample.telemetry)!=6 or any(row.get('torque')!=0 for row in sample.telemetry.values()):
                    raise ValueError('현재 팔의 동작을 멈추고 토크 OFF를 확인한 뒤 전환하세요.')
            self.live_adjust.stop(halt=False);self.stop_preview(quiet=True)
            if self.camera and self.camera.running:sessions.append(self.camera)
            if sessions:
                def close_readers():
                    for session in sessions:session.close()
                    deadline=time.monotonic()+4
                    while any(session.running for session in sessions) and time.monotonic()<deadline:time.sleep(.05)
                    if any(session.running for session in sessions):raise ValueError('기존 팔 연결 정리 중입니다. 전환하지 않았습니다.')
                self.arm_switch_job=(self.settings.pool.submit(close_readers),key,candidate)
                self.arm_selector.configure(state='disabled');self.notice('기존 읽기 연결 정리 후 '+ARM_NAMES[arm_id(candidate)]+' 선택 중')
            else:self.finish_arm_selection(key,candidate)
        finally:self.refresh_arm_selector()

    def poll_arm_selection(self):
        task=self.arm_switch_job
        if not task or not task[0].done():return
        self.arm_switch_job=None
        try:task[0].result();self.finish_arm_selection(task[1],task[2])
        finally:self.arm_selector.configure(state='readonly');self.refresh_arm_selector()

    def finish_arm_selection(self,key,profile):
        self.settings.require_idle()
        self.profile['preview_joint_angles_rad']=list(self.reference.angles(self.target))
        self.settings.library.save(self.profile['name'],self.profile,self.settings.active_profile_key)
        self.settings.active_profile_key=key;self.install_profile(profile)
        self.camera=None;self.camera_restart_pending=False;self.startup_devices_pending=False
        if calibration_pending(profile):
            self.show_page('settings');self.settings.tabs.select(self.settings.pages['calibration'])
            self.settings.vars['cal_role'].set('팔로워');self.settings.calibration_panel.sync_role()
            self.notice(ARM_NAMES[arm_id(profile)]+' 선택 · 새 3점 보정을 완료한 뒤 티칭·실행할 수 있습니다.')
        else:self.notice(ARM_NAMES[arm_id(profile)]+' 선택 · 이 팔의 에피소드와 보정값을 불러왔습니다.')
        self.refresh_arm_selector()

    def update_selected_arm_preview(self,previous_profile=None,previous_angles=None):
        poses=getattr(self,'arm_preview_poses',{})
        if previous_profile is not None:poses[arm_id(previous_profile)]=tuple(previous_angles)
        self.arm_preview_poses=poses
        other='arm2' if arm_id(self.profile)=='arm3' else 'arm3'
        default=self.workcell_preview['joint_angles_rad'] if self.workcell_preview else ()
        library=self.settings.library.items if hasattr(self,'settings') else {}
        other_profile=next((p for p in library.values() if arm_id(p)==other),{})
        default=other_profile.get('preview_joint_angles_rad',default)
        self.workcell_preview=scene_placement(self.workcell_preview,self.profile,poses.get(other,default))
        if self.workcell_preview and other_profile:
            from .arm_workspace import world_from_base
            z=float(world_from_base(other_profile)[2,3])
            if other=='arm2':self.workcell_preview['arm2_base_z_mm']=z
            else:self.workcell_preview['base_xyz_mm'][2]=z
        if self.workcell_preview and other_profile.get('tcp'):self.workcell_preview['other_arm_tcp']=deepcopy(other_profile['tcp'])
        self.refresh_arm_selector()
