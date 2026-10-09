"""Native workspace; live editing requires an explicit, scoped toggle."""
from copy import deepcopy
import io
import math
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk
from .ui_scroll import AutoScrollbar
from PIL import Image,ImageTk
import numpy as np
from .domain import ROOT,JOINTS,LABELS,load_profile,EpisodeStore,read_json,atomic_json
from .devices import CameraSession
from .motion import MotionSession,SPEED_PRESETS,leader_target
from .geometry import Kinematics
from .display_coordinates import display_position,display_heading
from .configuration import JigCatalog,model_tcp
from .playback import Playback
from .devices import ReadOnlySession
from .leader_assist import LeaderAssistSession
from .leader_assist_ui import LeaderAssistControls
from .arm_selection_ui import ArmSelectionControls
from .arm_workspace import arm_id,calibration_pending,require_calibrated,world_from_base
from .demo import DemoSession
from .workspace_features import WorkspaceFeatures
from .preview import PREVIEW_SIZE,Renderer,frame_delay_ms,pan_view,overhead_view,overhead_fovy
from .telemetry import monitor_tick,monitor_load

COLORS={'bg':'#eef2f6','panel':'#ffffff','ink':'#142333','muted':'#526477','border':'#c2ccd6','nav':'#132b3a',
        'accent':'#11697a','selected':'#d9eef1','danger':'#ad333d','viewport':'#dfe7ed'}

def styles(root):
    root.configure(bg=COLORS['bg']);root.option_add('*Listbox.font',('Noto Sans CJK KR',11));root.option_add('*Button.font',('Noto Sans CJK KR',11))
    owner=root._root()
    if getattr(owner,'so101_styles_ready',False):return
    owner.so101_styles_ready=True
    from tkinter import font as tkfont
    for name in ('TkDefaultFont','TkTextFont','TkMenuFont','TkHeadingFont','TkFixedFont','TkSmallCaptionFont','TkIconFont','TkTooltipFont'):
        font=tkfont.nametofont(name,root=root);size=font.actual('size');font.configure(size=size+1 if size>0 else size-1)
        if name!='TkFixedFont':font.configure(family='Noto Sans CJK KR')
    s=ttk.Style(root);s.theme_use('clam')
    s.configure('.',font=('Noto Sans CJK KR',11),background=COLORS['bg'],foreground=COLORS['ink'])
    s.configure('TFrame',background=COLORS['bg']);s.configure('Card.TFrame',background=COLORS['panel'])
    s.configure('TLabel',background=COLORS['panel'],foreground=COLORS['ink'])
    s.configure('Muted.TLabel',foreground=COLORS['muted']);s.configure('Title.TLabel',font=('Noto Sans CJK KR',22,'bold'),background=COLORS['bg'])
    s.configure('Section.TLabel',font=('Noto Sans CJK KR',14,'bold'));s.configure('Small.TLabel',font=('Noto Sans CJK KR',10),foreground=COLORS['muted'])
    s.configure('TButton',padding=(10,7),background='#f6f8fa',bordercolor=COLORS['border'],relief='raised',borderwidth=1)
    s.map('TButton',background=[('active','#e3ebf2'),('pressed','#d0dfe7')],foreground=[('disabled','#687986')])
    s.configure('Primary.TButton',background=COLORS['accent'],foreground='white',bordercolor=COLORS['accent'])
    s.map('Primary.TButton',background=[('active','#0c5362'),('disabled','#c6d7dc')],foreground=[('disabled','#526477')])
    s.configure('Compact.TButton',padding=(8,3))
    s.configure('Compact.Primary.TButton',padding=(8,3))
    s.configure('Stop.TButton',background='#a82e3b',foreground='white',bordercolor='#a82e3b')
    s.map('Stop.TButton',background=[('active','#89212d'),('disabled','#e5e9ed')],foreground=[('disabled','#687986')])
    s.configure('Jig.TCheckbutton',background='#e8f3f4',padding=(8,7),font=('Noto Sans CJK KR',11,'bold'))
    s.configure('Treeview',background='white',fieldbackground='white',rowheight=38,bordercolor=COLORS['border'])
    s.configure('Step.Treeview',rowheight=30)
    s.configure('Monitor.Treeview',rowheight=32)
    s.configure('Comparison.Treeview',rowheight=23,font=('Noto Sans CJK KR',10))
    s.configure('Comparison.Treeview.Heading',padding=2,font=('Noto Sans CJK KR',10,'bold'))
    s.configure('Inspection.Treeview',rowheight=26,font=('Noto Sans CJK KR',10))
    s.configure('Inspection.Treeview.Heading',padding=3,font=('Noto Sans CJK KR',10,'bold'))
    s.configure('Treeview.Heading',font=('Noto Sans CJK KR',10,'bold'),background='#edf3f6',padding=7)
    s.map('Treeview',background=[('selected',COLORS['selected'])],foreground=[('selected',COLORS['ink'])])
    s.configure('Horizontal.TScale',background=COLORS['accent'],troughcolor='#e1e9ef',bordercolor='#e1e9ef',lightcolor=COLORS['accent'],darkcolor=COLORS['accent'])
    for control in ('TEntry','TSpinbox','TCombobox'):
        s.configure(control,padding=(6,8),fieldbackground='white',bordercolor=COLORS['border'])
        s.map(control,fieldbackground=[('disabled','#edf1f4'),('readonly','#f6f8fa')],foreground=[('disabled','#687986')])
    s.configure('TNotebook',background=COLORS['bg'],bordercolor=COLORS['border'])
    s.configure('TNotebook.Tab',padding=(12,6))
    s.map('TNotebook.Tab',background=[('selected','white'),('!selected','#e5edf2')],foreground=[('selected',COLORS['accent'])])
    s.configure('Joint.TSpinbox',padding=(4,1))

def show_ready_window(root,*,minimized=False):
    """Map a fully laid-out window; keep the first paint hidden when compositing exists."""
    if minimized:
        root.update_idletasks();root.iconify();return
    alpha_supported=True
    try:root.attributes('-alpha',0.)
    except tk.TclError:alpha_supported=False
    root.update_idletasks()
    root.deiconify()
    root.update_idletasks()
    def maximize():
        if not root.winfo_exists():return
        try:root.attributes('-zoomed',True)
        except tk.TclError:
            try:root.state('zoomed')
            except tk.TclError:pass
    # Some window managers discard a zoom request made while withdrawn.
    root.after_idle(maximize)
    if alpha_supported:
        def reveal():
            try:
                if root.winfo_exists():root.attributes('-alpha',1.)
            except tk.TclError:pass  # Closed while its first frame was being prepared.
        root.after(34,reveal)

from .camera_lifecycle import CameraLifecycle
from .height_reference import DEFAULT_FLOOR_Z_MM,floor_z,floor_adjustment,floor_from_adjustment,profile_with_arm_height,restore_model_floor,support_height,support_bottom_z,support_plane_profile,detection_plane_z,height_from_base_z

class App(WorkspaceFeatures,CameraLifecycle,LeaderAssistControls,ArmSelectionControls):
    def __init__(self,root,data_dir=None,*,render=True,auto_camera=True,auto_devices=False,mode_override=None,profile_override=None,camera_only=False):
        self.camera_only=camera_only;self.inspection_run=None
        if camera_only:auto_devices=False
        self.workspace_manager=None;self.workspace_visible=True;self.camera_waiting_for_arm=False
        self.root=root;self.data_dir=Path(data_dir or ROOT/'data');self.profile,self.calibration,self.reference=load_profile(self.data_dir,profile=profile_override)
        from .workcell_preview import load_placement,placed_tcp_pose
        self.workcell_preview=load_placement(self.data_dir)
        self.workcell_tcp=placed_tcp_pose(self.workcell_preview) if self.workcell_preview else None
        self.linear_state_query=None
        self.startup_devices_pending=auto_devices;self.camera_auto_view=auto_camera
        self.jig_updates_paused=False;self.teaching_jig_results={};self.move_uses_held_jig=False
        self.camera_task=None;self.camera_sleeping=False;self.camera_manual_off=False;self.camera_close_requested=False;self.camera_view_requested=False
        if mode_override:self.profile['mode']=mode_override
        if self.profile.get('tcp',{}).get('mode','model')=='model':self.profile['tcp']=model_tcp(point=self.profile.get('tcp',{}).get('model_point','tip'))
        self.kin=Kinematics(self.reference,tcp=self.profile['tcp']);self.store=EpisodeStore(self.data_dir/'episodes',self.calibration,arm_id(self.profile))
        self.episode=self.store.new();self.selected=None;self.mode='target';self.page='teach';self.closed=False;self.job=None;self.render_job=None
        self.safe_editing=False;self.safe_draft=None;self.safe_entry=None;self.leader_session=None;self.camera_restart_pending=False;self.transport=Playback();self.pending_execution=None;self.play_targets=[];self.last_plan=[];self.session=None;self.camera=None;self.latest=None;self.playing=False;self.play_index=0;self.play_start=0
        self.render_enabled=render;self.renderer=None;self.last_render=None;self.last_rgb=None;self.render_context=0;self.pending_render_context=None;self.displayed_token=0;self.live_was_fresh=False;self.last_fk_angles=None;self.last_camera_at=0;self.photo=None
        self.view=overhead_view(self.profile);self.drag_start=None;self.roi_start=None;self.roi_points=[];self.roi_hover=None;self.camera_rect=None
        from .display_pose import DisplayPoses
        self.display_poses=DisplayPoses()
        self.camera_display_poses=DisplayPoses(duration=.12)
        from .vision import stl_profile
        from .vision import PoseLatch
        from .jig_consensus import saved_seconds,ACQUISITION_RANGE,ATTEMPT_RANGE,HOLD_RANGE
        self.pose_latch=PoseLatch()
        self.adjustments_path=self.data_dir/'model-adjustments.json'
        if self.adjustments_path.exists():
            adjustment=read_json(self.adjustments_path)
            if adjustment.get('calibration_sha256')==self.calibration.sha256:
                z=float(adjustment['table_z_mm'])
                if not math.isfinite(z) or not -100<=z<=100:raise ValueError('저장된 바닥 기준면 보정이 올바르지 않습니다.')
                self.reference.set_trims(dict.fromkeys(JOINTS,0) if self.calibration.angle_mapping else self.profile['model_reference'].get('trim_ticks',adjustment['trim_ticks']));self.profile=restore_model_floor(self.profile,adjustment);self.pose_latch.configure(saved_seconds(adjustment.get('hold_seconds',10.),HOLD_RANGE,10.))
        self.catalog=JigCatalog(self.data_dir,profile=self.profile,persist_migration=True);self.active_jig=next(iter(self.catalog.items));self.mesh_profile=self.catalog.mesh(self.active_jig)
        from .vision_service import MultiDetector
        self.detector=MultiDetector(self.catalog,self.profile,self.active_jig,self.pose_latch);self.detector.seconds=self.pose_latch.seconds
        self.target=self.reference.middle.copy();self.spins={};self.tick_vars={};self.current_vars={};self.sliders={};self.nav_buttons={}
        self.preferences=read_json(self.data_dir/'preferences.json') if (self.data_dir/'preferences.json').exists() else {}
        if self.preferences.get('motion_speed_version')!=2:
            self.preferences['motion_speed']='보통';self.preferences['motion_speed_version']=2
        self.pose_latch.stability.configure(saved_seconds(self.preferences.get('jig_acquisition_seconds',3.),ACQUISITION_RANGE,3.))
        self.detector.acquisition_seconds=self.pose_latch.stability.seconds
        self.measurement_attempts_limit=int(saved_seconds(self.preferences.get('jig_measurement_attempts',3),ATTEMPT_RANGE,3))
        self.pose_latch.stability.configure(self.pose_latch.stability.seconds,self.measurement_attempts_limit);self.detector.attempts_limit=self.measurement_attempts_limit
        self.remote=None;self.remote_mode=camera_only or self.preferences.get('device_host')=='pi';self.ui_heartbeat=time.monotonic();self.remote_plan_job=None
        self.roi=self.preferences.get('camera_roi') if self.preferences.get('camera_source')==self.profile['camera']['source'] else None;self.set_guard=False
        from .roi_geometry import roi_vertices
        try:roi_vertices(self.roi)
        except ValueError:self.roi=None
        if not self.catalog.path.exists():self.catalog.items[self.active_jig]['roi']=self.roi
        else:self.roi=self.catalog.items[self.active_jig].get('roi')
        styles(root);root.title('SO-101 Teach · 모터 틱 티칭');root.geometry('1480x920');root.minsize(1180,760)
        root.protocol('WM_DELETE_WINDOW',self.close);root.bind('<Escape>',lambda e:self.stop_preview())
        from .live_adjust import LiveAdjust
        self.live_adjust=LiveAdjust(self)
        self.build();self.update_selected_arm_preview();self.refresh_library();self.apply_target(self.target);self.show_page('teach')
        wanted=self.preferences.get('last_episode_by_arm',{}).get(arm_id(self.profile),self.preferences.get('last_episode_id'))
        if self.library_entries and not any(doc['id']==wanted for _,doc in self.library_entries):wanted=max((doc for _,doc in self.library_entries),key=lambda d:d.get('created_at',0))['id']
        for i,(_,doc) in enumerate(self.library_entries):
            if doc['id']==wanted:self.library.selection_set(i);self.load_selected_episode();break
        if mode_override:self.profile['mode']=mode_override;self.settings.fill_robot()
        if auto_devices and self.profile.get('mode')!='demo':
            self.guard(lambda:self.episode_sync.refresh(preserve_local_changes=True))
            self.start_linear_state_query()
            self.startup_job=self.root.after(200,self.startup_connections)
        self.poll()
        self.render_tick()
    def refresh_linear_status(self):
        from .linear_state import state_label
        placement=self.workcell_preview or {}
        self.linear_status.set(state_label(placement) if placement.get('linear_stage',{}).get('state_source') else '')
    def apply_linear_state(self,state):
        if not self.workcell_preview:return
        from .linear_state import apply_state
        self.workcell_preview=apply_state(self.workcell_preview,state)
        self.refresh_linear_status()
    def start_linear_state_query(self):
        if self.camera_only:return
        source=(self.workcell_preview or {}).get('linear_stage',{}).get('state_source')
        if self.closed or not self.remote_mode or self.profile.get('mode')=='demo' or not source:return
        if self.workspace_manager:self.workspace_manager.start_linear_state(self);return
        from .linear_state import StateStream
        from .pi_connection import load
        connection=load(self.data_dir);current=self.linear_state_query
        if current and not current.stop.is_set() and current.connection==connection and current.source==source:return
        if current:current.close()
        self.apply_linear_state({'known':False,'pending':True,'measured':False})
        self.linear_state_query=StateStream(connection,source);self.linear_state_query.start()
    def stop_linear_state(self):
        if self.workspace_manager:self.workspace_manager.stop_linear_state();return
        if self.linear_state_query:self.linear_state_query.close()
        self.apply_linear_state({'known':False,'connected':False,'measured':False})
    def poll_linear_state(self):
        if self.workspace_manager or not self.linear_state_query or self.linear_state_query.stop.is_set():return
        try:state=self.linear_state_query.results.get_nowait()
        except queue.Empty:return
        self.apply_linear_state(state)
    def startup_connections(self):
        if self.camera_only:return
        if self.closed or self.profile.get('mode')=='demo':return
        if self.episode_sync.job:
            self.startup_job=self.root.after(150,self.startup_connections);return
        if self.remote_mode:
            self.guard(self.settings.pi_panel.connect)
        else:
            self.connect_startup_devices()
    def startup_camera_only(self):
        if self.closed or not self.camera_only or not self.workspace_visible:return
        if self.episode_sync.job:
            self.startup_job=self.root.after(150,self.startup_camera_only);return
        self.startup_devices_pending=False;self.remote_mode=True
        self.camera_view_requested=True;self.camera_manual_off=False
        self.show_page('camera')
        self.guard(self.settings.pi_panel.connect)
        self.notice('카메라 전용 연결 · 모터 자동 연결과 동작 명령은 차단됩니다.')
    def connect_startup_devices(self):
        if self.camera_only:return
        if self.closed or not self.startup_devices_pending or self.profile.get('mode')=='demo':return
        self.startup_devices_pending=False
        if calibration_pending(self.profile):
            self.notice('선택한 로봇팔의 새 3점 보정이 필요합니다.');return
        self.guard(self.connect_devices)
    def card(self,parent,**kwargs):return ttk.Frame(parent,style='Card.TFrame',padding=16,**kwargs)
    def button(self,parent,text,command,primary=False,**kw):
        return ttk.Button(parent,text=text,command=lambda:self.guard(command),style='Primary.TButton' if primary else 'TButton',**kw)
    def guard(self,command,*,during_shutdown=False):
        if not during_shutdown and getattr(getattr(self.workspace_manager,'safe_shutdown',None),'busy',False):
            self.notice('안전 종료 진행 중 · Esc로 복귀를 중단할 수 있습니다.');return
        try:return command()
        except Exception as exc:self.notice(str(exc),True)
    def notice(self,text,error=False):
        self.message.set(text);self.message_label.configure(foreground=COLORS['danger'] if error else COLORS['muted'])
    def device_notice(self,text,error=False):
        self.device_message.set(text)
        if not getattr(self,'last_motion_stop',None):self.notice(text,error)
    def build(self):
        self.root.rowconfigure(0,weight=1);self.root.columnconfigure(1,weight=1)
        nav=tk.Frame(self.root,bg=COLORS['nav'],width=150);nav.grid(row=0,column=0,sticky='ns');nav.grid_propagate(False)
        tk.Label(nav,text='SO—101',bg=COLORS['nav'],fg='white',font=('Noto Sans CJK KR',21,'bold')).pack(anchor='w',padx=18,pady=(26,0))
        tk.Label(nav,text='TEACH WORKSPACE',bg=COLORS['nav'],fg='#b5ccd7',font=('Noto Sans CJK KR',9)).pack(anchor='w',padx=18,pady=(0,30))
        for key,title in [('teach','01   티칭'),('camera','02   상단 카메라'),('devices','03   장비 모니터링'),('model','04   모델·계산 보정'),('settings','05   설정 · 안내')]:
            b=tk.Button(nav,text=title,anchor='w',padx=15,pady=14,relief='flat',bd=0,bg=COLORS['nav'],fg='#d0e1e8',activebackground='#245465',activeforeground='white',takefocus=True,
                        command=lambda k=key:self.show_page(k));b.pack(fill='x',padx=10,pady=4);self.nav_buttons[key]=b
        self.device_target=tk.StringVar(value='장치: Pi · 미연결' if self.remote_mode else '장치: 이 PC')
        tk.Label(nav,textvariable=self.device_target,justify='left',bg=COLORS['nav'],fg='#b5ccd7',font=('Noto Sans CJK KR',10)).pack(side='bottom',anchor='w',padx=18,pady=25)
        shell=ttk.Frame(self.root,padding=(22,10));shell.grid(row=0,column=1,sticky='nsew');shell.rowconfigure(2,weight=1);shell.columnconfigure(0,weight=1)
        header=ttk.Frame(shell);header.grid(row=0,column=0,sticky='ew',pady=(0,6));header.columnconfigure(0,weight=1)
        self.page_title=tk.StringVar(value='티칭 워크스페이스');ttk.Label(header,textvariable=self.page_title,style='Title.TLabel').grid(row=0,column=0,sticky='w')
        self.build_arm_selector(header)
        self.connection=tk.StringVar(value='모터 미연결');self.badge=ttk.Label(header,textvariable=self.connection,padding=(12,8));self.badge.grid(row=0,column=2,padx=8)
        self.connect_btn=self.button(header,'팔로워 연결',self.toggle_connection,True);self.connect_btn.grid(row=0,column=3)
        banner=tk.Frame(shell,bg='#e3ebf0',padx=12,pady=4);banner.grid(row=1,column=0,sticky='ew',pady=(0,8))
        self.guidance=tk.StringVar(value='스텝을 선택하고 자세를 편집하세요. 오른쪽 3D에서 확인한 뒤 저장하거나 실행합니다.')
        tk.Label(banner,textvariable=self.guidance,bg='#e3ebf0',fg=COLORS['muted'],anchor='w').pack(fill='x')
        self.body=ttk.Frame(shell);self.body.grid(row=2,column=0,sticky='nsew');self.body.rowconfigure(0,weight=1);self.body.columnconfigure(0,weight=1)
        self.pages={key:ttk.Frame(self.body) for key in ('teach','camera','devices','model','settings')}
        for frame in self.pages.values():frame.grid(row=0,column=0,sticky='nsew')
        self.build_teach(self.pages['teach']);self.build_camera(self.pages['camera']);self.build_devices(self.pages['devices']);self.build_model(self.pages['model'])
        from .settings_ui import SettingsPanel
        self.settings=SettingsPanel(self,self.pages['settings'])
        from .pi_episode_library import PiEpisodeLibrary
        self.episode_sync=PiEpisodeLibrary(self)
        self.refresh_episode_policy()
        self.root.bind('<F1>',lambda e:self.open_help());self.root.bind('<space>',self.space_capture);self.root.bind('<KeyRelease-space>',self.space_key_release)
        for key in ('z','Z'):
            self.root.bind(f'<KeyPress-{key}>',self.jig_key_press)
            self.root.bind(f'<KeyRelease-{key}>',self.jig_key_release)
        self.root.bind('<FocusOut>',self.cancel_jig_key,add='+')
        self.root.bind('<FocusOut>',self.cancel_space_key,add='+')
        footer=ttk.Frame(shell);footer.grid(row=3,column=0,sticky='ew',pady=(8,0));footer.columnconfigure(0,weight=1)
        self.message=tk.StringVar(value='팔로워를 연결해 현재 틱을 확인하거나, 저장할 목표값을 편집하세요.')
        self.message_label=tk.Label(footer,textvariable=self.message,font=('Noto Sans CJK KR',11),fg=COLORS['muted'],wraplength=900,background=COLORS['bg'],height=2,width=1,anchor='w',justify='left',bd=0,padx=0,pady=0);self.message_label.grid(row=0,column=0,sticky='ew')
        self.message_label.bind('<Configure>',lambda e:self.message_label.configure(wraplength=max(200,e.width)))
        self.linear_status_label=tk.Label(footer,textvariable=self.linear_status,font=('Noto Sans CJK KR',10),fg=COLORS['muted'],bg=COLORS['bg'])
        if self.linear_status.get():self.linear_status_label.grid(row=0,column=1,sticky='e',padx=(16,0))
        controls=ttk.Frame(footer);controls.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(4,0))
        self.arm_btn=self.button(controls,'현재 자세 유지 · 토크 켜기',lambda:self.motion_request('arm'));self.arm_btn.pack(side='left',padx=(0,6))
        self.hold_btn=self.button(controls,'동작 정지 · 자세 유지',lambda:self.motion_request('hold'));self.hold_btn.configure(style='Stop.TButton',command=lambda:self.guard(lambda:self.motion_request('hold'),during_shutdown=True));self.hold_btn.pack(side='left',padx=6)
        self.release_btn=self.button(controls,'토크 해제',lambda:self.motion_request('release'));self.release_btn.pack(side='left',padx=6)
        self.teach_reread_jig_btn=self.button(controls,'지그 다시 읽기',self.request_jig_read)
        self.teach_reread_jig_btn.pack(side='right')
        self.jig_pause_btn=self.button(controls,'자동 갱신 정지',self.toggle_jig_updates)
        self.jig_pause_btn.pack(side='right',padx=(0,8))
        self.root.bind('<Escape>',lambda e:self.stop_all())
        self.follow_btn=self.button(controls,'리더 따라가기',lambda:self.motion_request('follow'));self.follow_btn.pack(side='left',padx=6)
    def build_teach(self,parent):
        outer=parent;outer.rowconfigure(0,weight=1);outer.columnconfigure(0,weight=1)
        self.teach_tabs=ttk.Notebook(outer);self.teach_tabs.grid(row=0,column=0,sticky='nsew')
        self.teach_overview=ttk.Frame(self.teach_tabs);self.episode_adjust=ttk.Frame(self.teach_tabs)
        for frame,title in ((self.teach_overview,'스텝 추가·편집'),(self.episode_adjust,'에피소드 조정')):
            self.teach_tabs.add(frame,text=title);frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
        self.teach_workbench=parent=ttk.Frame(outer)
        parent.grid(in_=self.teach_overview,row=0,column=0,sticky='nsew',pady=(8,0))
        parent.rowconfigure(0,weight=1);parent.columnconfigure(1,weight=1)
        left=self.card(parent);left.grid(row=0,column=0,sticky='nsew',padx=(0,12));left.configure(width=275,padding=12);left.grid_propagate(False);left.columnconfigure(0,weight=1);left.rowconfigure(7,weight=1)
        ttk.Label(left,text='에피소드',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.library=tk.Listbox(left,height=2,bd=0,highlightthickness=1,highlightbackground=COLORS['border'],selectbackground=COLORS['selected'],selectforeground=COLORS['ink'],exportselection=False)
        self.library.grid(row=1,column=0,sticky='ew',pady=(2,2));self.library.bind('<<ListboxSelect>>',self.load_selected_episode)
        self.episode_name=tk.StringVar(value=self.episode['name']);self.episode_name_entry=ttk.Entry(left,textvariable=self.episode_name);self.episode_name_entry.grid(row=2,column=0,sticky='ew')
        row=ttk.Frame(left,style='Card.TFrame');row.grid(row=3,column=0,sticky='ew',pady=2)
        self.episode_edit_actions=row
        self.button(row,'새로',self.new_episode,width=3).pack(side='left',fill='x',expand=True,padx=(0,5));self.button(row,'저장',self.save_episode,True,width=3).pack(side='left',fill='x',expand=True)
        self.button(row,'복사',self.save_as_episode,width=3).pack(side='left',fill='x',expand=True,padx=(5,0))
        self.delete_episode_btn=self.button(row,'삭제',self.request_episode_delete,width=3);self.delete_episode_btn.pack(side='left',fill='x',expand=True,padx=(5,0))
        self.export_episode_btn=self.button(left,'에피소드 조정',lambda:self.settings.episode_transfer_panel.open() if self.episode_adjusting() else self.open_episode_adjust())
        self.export_episode_btn.configure(style='Compact.TButton');self.export_episode_btn.grid(row=4,column=0,sticky='ew',pady=2)
        self.step_list_title=ttk.Label(left,text='스텝',style='Section.TLabel');self.step_list_title.grid(row=5,column=0,sticky='w')
        sync=ttk.Frame(left,style='Card.TFrame');sync.grid(row=6,column=0,sticky='ew',pady=(0,2))
        self.pi_episode_status=tk.StringVar(value='Pi 에피소드 확인 전')
        self.pi_episode_status_label=tk.Label(sync,textvariable=self.pi_episode_status,font=('Noto Sans CJK KR',10),fg=COLORS['muted'],bg=COLORS['panel'],wraplength=240,height=2,anchor='w',justify='left');self.pi_episode_status_label.pack(fill='x')
        self.pull_episodes_btn=self.button(sync,'Pi에서 다시 불러오기',lambda:self.episode_sync.refresh());self.pull_episodes_btn.pack(fill='x',pady=(3,0))
        step_list=ttk.Frame(left);step_list.grid(row=7,column=0,sticky='nsew');step_list.rowconfigure(0,weight=1);step_list.columnconfigure(0,weight=1)
        self.steps=ttk.Treeview(step_list,style='Step.Treeview',columns=('name','reference','completion'),show='headings',selectmode='browse');self.steps.heading('name',text='스텝');self.steps.column('name',width=110,minwidth=85);self.steps.heading('reference',text='기준');self.steps.column('reference',width=38,minwidth=38,stretch=False)
        self.steps.heading('completion',text='알림·검사');self.steps.column('completion',width=88,minwidth=85,stretch=False)
        self.steps.grid(row=0,column=0,sticky='nsew');step_scroll=AutoScrollbar(step_list,orient='vertical',command=self.steps.yview);step_scroll.grid(row=0,column=1,sticky='ns');self.steps.configure(yscrollcommand=step_scroll.set)
        self.empty_steps=ttk.Label(self.steps,text='아직 스텝이 없습니다.\n자세 편집 → 새 스텝 추가\n또는 실물 자세 추가',style='Small.TLabel',justify='center');self.empty_steps.place(relx=.5,y=70,anchor='n');self.steps.bind('<<TreeviewSelect>>',self.select_step)
        self.steps.bind('<ButtonRelease-1>',self.drop_step);self.steps.bind('<ButtonPress-1>',self.left_step_press)
        extra=ttk.Frame(left);extra.grid(row=9,column=0,sticky='ew',pady=4)
        self.step_edit_actions=extra
        self.safe_edit_btn=self.button(extra,'안전 자세 설정',self.begin_safe_edit);self.safe_edit_btn.pack(side='left',fill='x',expand=True)
        self.delete_step_btn=self.button(extra,'스텝 삭제',self.delete_step);self.delete_step_btn.pack(side='left',fill='x',expand=True,padx=(4,0))
        self.teach_preview_btn=self.button(left,'경로 미리보기',self.play);self.teach_preview_btn.grid(row=10,column=0,sticky='ew',pady=(4,0))
        speed_row=ttk.Frame(left,style='Card.TFrame');speed_row.grid(row=11,column=0,sticky='ew',pady=(6,0))
        self.motion_speed_row=speed_row
        ttk.Label(speed_row,text='스텝 속도').pack(side='left')
        self.motion_speed_choice=ttk.Combobox(speed_row,values=list(SPEED_PRESETS),state='readonly',width=8);self.motion_speed_choice.pack(side='right')
        saved_speed=self.preferences.get('motion_speed','보통');self.motion_speed_choice.set(saved_speed if saved_speed in SPEED_PRESETS else '보통')
        self.motion_speed_choice.bind('<<ComboboxSelected>>',lambda e:self.guard(self.change_motion_speed))
        self.execute_btn=self.button(left,'에피소드 실물 실행',self.execute_episode,True);self.execute_btn.grid(row=12,column=0,sticky='ew',pady=(6,0))
        self.execute_taught_btn=self.button(left,'티칭 그대로 실행 · 보정 없음',self.execute_taught_episode);self.execute_taught_btn.grid(row=13,column=0,sticky='ew',pady=(4,0))
        self.execute_taught_btn.state(['disabled'])
        def compact_actions(parent):
            for child in parent.winfo_children():
                if isinstance(child,ttk.Button):child.configure(style='Compact.Primary.TButton' if child.cget('style')=='Primary.TButton' else 'Compact.TButton')
                elif isinstance(child,ttk.Frame):compact_actions(child)
        compact_actions(left)
        self.workspace=ttk.Frame(parent);self.workspace.grid(row=0,column=1,sticky='nsew');self.workspace.columnconfigure(0,minsize=310);self.workspace.columnconfigure(2,weight=1);self.workspace.rowconfigure(0,weight=1)
        preview_card=self.card(self.workspace);preview_card.grid(row=0,column=2,sticky='nsew');preview_card.columnconfigure(0,weight=1);preview_card.rowconfigure(2,weight=1)
        top=ttk.Frame(preview_card,style='Card.TFrame');top.grid(row=0,column=0,sticky='ew');top.columnconfigure(0,weight=1)
        ttk.Label(top,text='3D 자세 확인',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.button(top,'시점 복원',self.reset_view).grid(row=0,column=1)
        if self.workcell_preview:self.button(top,'전체 작업대',self.workcell_view).grid(row=0,column=2,padx=(4,0))
        modes=ttk.Frame(preview_card,style='Card.TFrame');modes.grid(row=1,column=0,sticky='ew',pady=(6,6))
        self.live_btn=self.button(modes,'실물값 보기',lambda:self.set_mode('live'));self.live_btn.pack(side='left',fill='x',expand=True,padx=(0,6))
        self.target_btn=self.button(modes,'스텝 자세',lambda:self.set_mode('target'),True);self.target_btn.pack(side='left',fill='x',expand=True)
        self.linear_status=tk.StringVar(value='')
        self.refresh_linear_status()
        self.preview_canvas=tk.Canvas(preview_card,bg=COLORS['viewport'],highlightthickness=0);self.preview_canvas.grid(row=2,column=0,sticky='nsew')
        self.preview_canvas.bind('<Configure>',lambda e:self.fit_image(self.preview_canvas,self.last_rgb) if self.last_rgb is not None and (self.mode!='live' or self.latest and self.latest.fresh()) else None)
        self.preview_canvas.bind('<ButtonPress-1>',lambda e:setattr(self,'drag_start',(e.x,e.y,self.view)))
        self.preview_canvas.bind('<B1-Motion>',self.rotate_view);self.preview_canvas.bind('<Button-4>',lambda e:self.zoom(.9));self.preview_canvas.bind('<Button-5>',lambda e:self.zoom(1.1))
        self.bind_pan(self.preview_canvas)
        self.preview_metadata=ttk.Frame(preview_card,style='Card.TFrame');self.preview_metadata.grid(row=3,column=0,sticky='ew',pady=(8,4));self.preview_metadata.columnconfigure(0,weight=1)
        self.preview_caption=tk.StringVar(value='스텝 자세 · 실물 구동 없음')
        self.tcp_label=tk.StringVar(value='TCP —')
        self.preview_tcp_label=ttk.Label(self.preview_metadata,textvariable=self.tcp_label,style='Small.TLabel',justify='left');self.preview_tcp_label.grid(row=0,column=0,sticky='w')
        self.preview_guide=ttk.Label(self.preview_metadata,text='드래그: 회전 · 우/중클릭: 이동 · 휠: 확대\n파랑: 원점 · 주황: TCP · 원: 지그 중심',style='Small.TLabel',justify='right')
        self.preview_guide.grid(row=0,column=1,sticky='ne',padx=(12,0))
        transport=self.preview_transport=ttk.Frame(preview_card,style='Card.TFrame');transport.grid(row=5,column=0,sticky='ew',pady=6);transport.columnconfigure(0,weight=1)
        self.timeline=ttk.Scale(transport,from_=0,to=1,command=self.seek_preview);self.timeline.grid(row=0,column=0,columnspan=4,sticky='ew')
        self.transport_label=tk.StringVar(value='0.0 / 0.0초');self.transport_time_label=ttk.Label(transport,textvariable=self.transport_label);self.transport_time_label.grid(row=1,column=0,sticky='w')
        self.pause_btn=self.button(transport,'일시정지',self.pause_preview);self.pause_btn.grid(row=1,column=1)
        self.preview_reset_btn=self.button(transport,'처음으로',lambda:self.seek_preview(0));self.preview_reset_btn.grid(row=1,column=2,padx=3)
        from .jig_comparison import JigComparisonPanel
        self.jig_comparison=JigComparisonPanel(preview_card,collapsed=self.preferences.get('jig_comparison_collapsed',False),on_toggle=self.save_comparison_visibility);self.jig_comparison.grid(row=7,column=0,sticky='ew',pady=(8,0))
        self.jig_comparison.on_view=self.update_live_jig_comparison
        self.comparison_reports={}
        self.taught_jig_display=None;self.preview_jig_references=None;self.measured_jig_display=None;self.measured_target_active=False
        from .step_editor import build_editor
        self.editor_card=build_editor(self,self.workspace)
        self.editor_card.grid(row=0,column=0,sticky='nsew',padx=(0,12))
        self.live_adjust.stop()
        self.second_editor=None;self.preview_editor=1
        self.teach_list_card=left;self.preview_card=preview_card
        self.episode_policy_card=self.card(self.workspace);self.episode_policy_card.columnconfigure(0,weight=1)
        self.episode_policy=tk.StringVar(value='Pi 에피소드를 불러오면 완료 알림과 실행 기준을 표시합니다.')
        from .episode_adjust_ui import EpisodeAdjustPanel
        self.episode_adjust_panel=EpisodeAdjustPanel(self,self.episode_policy_card)
        self.teach_tabs.bind('<<NotebookTabChanged>>',self.change_teach_tab)
        self.change_teach_tab()
        self.steps.tag_configure('editor2',background='#f3e4fc',foreground='#603b79')
        self.steps.bind('<Button-3>',self.open_second_editor)
        self.root.bind('<Button-3>',self.outside_editor_right_click,add='+')
        self.root.bind('<Button-1>',self.editor_focus_event,add='+')
        self.root.bind('<Button-1>',self.teaching_pointer_focus,add='+')
        self.root.bind('<Return>',self.finish_teaching_title,add='+')
        self.root.bind('<<ComboboxSelected>>',self.finish_teaching_selection,add='+')
        self.install_teaching_shortcuts(parent)
        self.root.bind('<FocusIn>',self.editor_focus_event,add='+')
        parent.bind('<Configure>',self.fit_teach_columns)
        preview_card.bind('<Configure>',self.fit_preview_text)
    def episode_adjusting(self):
        return self.teach_tabs.select()==str(self.episode_adjust)
    def open_episode_adjust(self):
        self.show_page('teach');self.teach_tabs.select(self.episode_adjust)
    def change_teach_tab(self,event=None):
        if not hasattr(self,'episode_policy_card'):return
        adjusting=self.episode_adjusting()
        self.live_adjust.stop()
        if adjusting:self.close_second_editor()
        self.teach_workbench.grid(in_=self.episode_adjust if adjusting else self.teach_overview,row=0,column=0,sticky='nsew',pady=(8,0))
        for widget in (self.editor_card,self.episode_name_entry,self.episode_edit_actions,self.step_edit_actions):
            widget.grid_remove() if adjusting else widget.grid()
        for widget in (self.motion_speed_row,self.execute_btn,self.execute_taught_btn):
            widget.grid_remove() if adjusting else widget.grid()
        if adjusting:
            self.preview_card.grid();self.episode_policy_card.grid(row=0,column=0,columnspan=1,sticky='nsew',padx=(0,12))
            self.episode_adjust_panel.refresh()
        else:self.episode_policy_card.grid_remove();self.preview_card.grid()
        self.step_list_title.configure(text='알림·검사 스텝 선택' if adjusting else '스텝 · 끌어서 순서 변경')
        self.export_episode_btn.configure(text='Pi 에피소드 내보내기' if adjusting else '에피소드 조정')
        if adjusting:self.pull_episodes_btn.pack(fill='x',pady=(3,0))
        else:self.pull_episodes_btn.pack_forget()
        self.fit_teach_columns()
        self.guidance.set('완제품·안착 검사·완료 알림·실행 설정 · 저장 후 Pi에 적용' if adjusting else '스텝 추가·자세 편집 · 알림과 실행 설정은 에피소드 조정 탭에서 변경')
    def refresh_episode_policy(self):
        if not hasattr(self,'episode_policy'):return
        from .episode_events import events_for,EVENT_LABELS
        events=events_for(self.episode);lines=[]
        snapshot=getattr(getattr(self,'episode_sync',None),'snapshot',None)
        if snapshot:
            settings=snapshot['settings'];slots=[k for k,v in snapshot['recipes'].items() if v['episode_id']==self.episode['id']]
            lines += [('Pi '+('/'.join(slots) or '보관 에피소드')),f"속도 {settings['speed']:g}틱/초",f"지그 측정 {settings['acquisition_seconds']:g}초 × {settings['acquisition_attempts']}회"]
            policy=snapshot.get('execution_policy',{})
            if policy:lines += [f"리니어 목표 {policy['linear_target_mm']:g}mm",f"종료 후 {policy['finish_hold_seconds']:g}초 유지 · 토크 해제"]
        lines.append('\n완료 알림'+(' · Pi 이름 규칙' if 'completion_events' not in self.episode else ' · 지정한 스텝'))
        for index,step in enumerate(self.episode['steps'],1):
            for kind,key in events.items():
                if key==step['id']:lines.append(f'{index:02} · {EVENT_LABELS[kind]}')
        if not events:lines.append('지정 없음')
        self.episode_policy.set('\n'.join(lines))
        if hasattr(self,'episode_adjust_panel'):self.episode_adjust_panel.refresh()
    def left_step_press(self,event):
        self.drag_step=self.steps.identify_row(event.y)
        if self.drag_step:self.preview_editor=1;self.set_mode('target');self.last_render=None
    def teaching_jig_id(self):
        editor=self.second_editor if self.preview_editor==2 and self.second_editor else self
        return editor.selected_jig_id()
    def editor_for_widget(self,widget):
        right=getattr(self,'second_editor',None)
        while widget is not None:
            if right and widget==right.editor_card:return right
            if widget==self.editor_card:return self
            widget=getattr(widget,'master',None)
        return self
    def editor_focus_event(self,event):
        if self.page!='teach':return
        editor=self.editor_for_widget(event.widget)
        if editor is not self:editor.activate()
        elif str(event.widget).startswith(str(self.editor_card)):
            self.preview_editor=1;self.last_render=None
    def fit_teach_columns(self,event=None):
        width=self.pages['teach'].winfo_width()
        wide=width>=1500
        list_width=450 if wide else 275
        self.steps.column('reference',width=56 if wide else 38)
        self.steps.column('completion',width=110 if wide else 88)
        self.pi_episode_status_label.configure(wraplength=list_width-24,height=0 if wide else 2)
        if self.episode_adjusting():
            self.teach_list_card.configure(width=list_width)
            self.workspace.columnconfigure(0,minsize=620 if wide else 660,weight=0);self.workspace.columnconfigure(1,minsize=0,weight=0);self.workspace.columnconfigure(2,minsize=0,weight=1)
            self.episode_policy_card.configure(width=608 if wide else 648,padding=12);self.episode_policy_card.grid_propagate(False)
            self.preview_card.grid_propagate(False)
            return
        dual=self.second_editor is not None
        if dual:
            list_width=max(450 if wide else 215 if width<1150 else 240,self.safe_edit_btn.winfo_reqwidth()+self.delete_step_btn.winfo_reqwidth()+28,self.execute_taught_btn.winfo_reqwidth()+24)
            editor_width=250 if width<1150 else 280
            self.teach_list_card.configure(width=list_width)
            for column in (0,1):self.workspace.columnconfigure(column,minsize=editor_width,weight=0)
            for card in (self.editor_card,self.second_editor.editor_card):
                card.configure(width=editor_width-12,padding=(8,12));card.grid_propagate(False)
            self.preview_card.grid_propagate(False)
        else:
            self.teach_list_card.configure(width=list_width)
            self.workspace.columnconfigure(0,minsize=310,weight=0);self.workspace.columnconfigure(1,minsize=0,weight=0)
            self.editor_card.configure(padding=(16,12));self.editor_card.grid_propagate(True)
            self.preview_card.grid_propagate(not wide)
        narrow=dual and width<1150
        self.transport_time_label.grid_configure(row=1,column=0,columnspan=4 if narrow else 1)
        self.pause_btn.grid_configure(row=2 if narrow else 1,column=0 if narrow else 1,columnspan=2 if narrow else 1)
        self.preview_reset_btn.grid_configure(row=2 if narrow else 1,column=2,columnspan=2 if narrow else 1)
    def fit_preview_text(self,event):
        width=max(120,event.width-32)
        if width>=640:
            right=min(340,int(width*.45));left=width-right-12
            self.preview_guide.grid_configure(row=0,column=1,rowspan=1,columnspan=1,sticky='ne',padx=(12,0),pady=0)
            self.preview_guide.configure(wraplength=right,justify='right')
        else:
            left=width
            self.preview_guide.grid_configure(row=1,column=0,rowspan=1,columnspan=2,sticky='e',padx=0,pady=(4,0))
            self.preview_guide.configure(wraplength=width,justify='right')
        self.preview_tcp_label.configure(wraplength=left)
    def open_second_editor(self,event):
        if self.episode_adjusting():return 'break'
        self.live_adjust.stop()
        key=self.steps.identify_row(event.y)
        if not key:return 'break'
        if self.second_editor and self.second_editor.selected==key:
            self.close_second_editor();return 'break'
        from .step_editor import SecondEditor
        if self.second_editor is None:self.second_editor=SecondEditor(self)
        self.second_editor.load(key);self.fit_teach_columns();self.mark_editor_steps()
        self.root.after_idle(lambda:self.fit_teach_columns() if not self.closed else None)
        return 'break'
    def close_second_editor(self):
        editor=getattr(self,'second_editor',None)
        if editor is None:return
        self.live_adjust.stop()
        self.second_editor=None;self.preview_editor=1
        editor.editor_card.destroy();self.fit_teach_columns();self.mark_editor_steps();self.last_render=None
    def outside_editor_right_click(self,event):
        if self.page!='teach' or self.second_editor is None:return
        widget=event.widget
        while widget is not None:
            if widget==self.steps:return
            widget=getattr(widget,'master',None)
        self.close_second_editor()
    def mark_editor_steps(self):
        from .episode_events import event_label
        editor=getattr(self,'second_editor',None)
        shared=editor is not None and editor.selected==self.selected
        ttk.Style(self.root).map('Step.Treeview',background=[('selected','#f3e4fc' if shared else COLORS['selected'])],foreground=[('selected','#603b79' if shared else COLORS['ink'])])
        for i,step in enumerate(self.episode['steps'],1):
            if not self.steps.exists(step['id']):continue
            right=bool(editor and editor.selected==step['id'])
            name=f'{i:02}   '+step['name']+(' · 데모' if step.get('source',{}).get('kind')=='demo' else '')
            marker=event_label(self.episode,step['id'])
            if 'inspection' in step:marker='안착 검사' if marker=='없음' else marker.replace(' 완료','')+' · 검사'
            self.steps.item(step['id'],values=(name,'지그' if step.get('jig_id') else '고정','—' if marker=='없음' else marker),tags=('editor2',) if right else ())
    def build_camera(self,parent):
        self.camera_tabs=ttk.Notebook(parent);self.camera_tabs.pack(fill='both',expand=True)
        detection=ttk.Frame(self.camera_tabs);self.camera_tabs.add(detection,text='지그 검출')
        from .camera_inspection_ui import CameraInspectionPanel
        self.camera_inspection=CameraInspectionPanel(self.camera_tabs,self)
        self.camera_tabs.add(self.camera_inspection,text='부품·조립 검사')
        parent=detection
        parent.columnconfigure(0,weight=1);parent.rowconfigure(0,weight=1)
        card=self.card(parent);card.grid(row=0,column=0,sticky='nsew');card.columnconfigure(0,weight=1);card.rowconfigure(1,weight=1)
        bar=ttk.Frame(card,style='Card.TFrame');bar.grid(row=0,column=0,sticky='ew',pady=(0,12))
        self.camera_jig_choice=ttk.Combobox(bar,state='readonly',width=16,values=['전체 지그',*[d['name'] for d in self.catalog.items.values()]]);self.camera_jig_choice.current(1);self.camera_jig_choice.grid(row=0,column=0,sticky='ew',padx=(0,8));self.camera_jig_choice.bind('<<ComboboxSelected>>',lambda e:self.guard(self.select_camera_jig))
        for i,(title,cb) in enumerate([('카메라 연결',self.start_camera),('카메라 해제',self.stop_camera),('영상 저장',self.save_camera),('저장 폴더 열기',self.open_capture_folder)],1):self.button(bar,title,cb).grid(row=i//4,column=i%4,sticky='ew',padx=(0,8),pady=3)
        hold=ttk.Frame(bar,style='Card.TFrame');hold.grid(row=1,column=1,columnspan=3,sticky='w')
        self.hold_seconds=tk.StringVar(value=str(self.pose_latch.seconds))
        self.acquisition_seconds=tk.StringVar(value=str(self.pose_latch.stability.seconds))
        self.measurement_attempts=tk.StringVar(value=str(self.measurement_attempts_limit))
        ttk.Label(hold,text='획득 시간 (초)').pack(side='left',padx=(0,5))
        self.acquisition_seconds_spin=ttk.Spinbox(hold,from_=3,to=10,increment=.5,textvariable=self.acquisition_seconds,width=5);self.acquisition_seconds_spin.pack(side='left',padx=(0,10))
        ttk.Label(hold,text='최대 시도 (회)').pack(side='left',padx=(0,5))
        self.measurement_attempts_spin=ttk.Spinbox(hold,from_=1,to=5,increment=1,textvariable=self.measurement_attempts,width=5);self.measurement_attempts_spin.pack(side='left',padx=(0,10))
        ttk.Label(hold,text='실행 기준 유지 (초)').pack(side='left',padx=(0,5))
        self.hold_seconds_spin=ttk.Spinbox(hold,from_=1,to=10,increment=1,textvariable=self.hold_seconds,width=5);self.hold_seconds_spin.pack(side='left')
        self.button(hold,'적용',self.save_camera_hold).pack(side='left',padx=(5,0))
        ttk.Label(hold,text='새 관측 최소 2회',style='Small.TLabel').pack(side='left',padx=(10,0))
        roi_bar=ttk.Frame(bar,style='Card.TFrame');roi_bar.grid(row=2,column=0,columnspan=4,sticky='ew',pady=(5,0))
        self.camera_all=tk.BooleanVar(value=bool(self.preferences.get('camera_all_jigs',False)))
        self.show_adoption=tk.BooleanVar(value=bool(self.preferences.get('camera_show_adoption',False)))
        self.roi_mode=tk.StringVar(value=self.preferences.get('roi_draw_mode','자율 영역'))
        if self.roi_mode.get() not in ('사각형','자율 영역'):self.roi_mode.set('자율 영역')
        ttk.Label(roi_bar,text='범위 지정',style='Small.TLabel').pack(side='left',padx=(0,6))
        self.roi_mode_choice=ttk.Combobox(roi_bar,state='readonly',width=10,values=('사각형','자율 영역'),textvariable=self.roi_mode);self.roi_mode_choice.pack(side='left',padx=(0,8))
        self.roi_mode_choice.bind('<<ComboboxSelected>>',lambda e:self.change_roi_mode())
        self.roi_cancel_button=self.button(roi_bar,'그리기 취소',self.cancel_roi_points);self.roi_cancel_button.pack(side='left',padx=(0,6))
        self.roi_clear_button=self.button(roi_bar,'범위 해제',self.clear_camera_roi);self.roi_clear_button.pack(side='left',padx=(0,8))
        self.roi_progress=tk.StringVar(value='드래그 후 놓으면 저장')
        ttk.Label(roi_bar,textvariable=self.roi_progress,style='Small.TLabel').pack(side='left',padx=(0,8))
        self.adoption_toggle=ttk.Checkbutton(roi_bar,text='채택 상태 표시',variable=self.show_adoption,command=self.toggle_adoption_display);self.adoption_toggle.pack(side='right',padx=(12,0))
        self.camera_view_area=ttk.Frame(card,style='Card.TFrame');self.camera_view_area.grid(row=1,column=0,sticky='nsew');self.camera_view_area.columnconfigure(0,weight=1);self.camera_view_area.rowconfigure(0,weight=1)
        self.camera_canvas=tk.Canvas(self.camera_view_area,bg=COLORS['nav'],highlightthickness=0);self.camera_canvas.grid(row=0,column=0,sticky='nsew')
        self.camera_canvas.bind('<ButtonPress-1>',self.roi_press);self.camera_canvas.bind('<B1-Motion>',self.roi_move);self.camera_canvas.bind('<ButtonRelease-1>',self.roi_release);self.camera_canvas.bind('<Button-3>',lambda e:self.cancel_roi_points())
        self.camera_overview=ttk.Frame(self.camera_view_area,style='Card.TFrame',padding=(12,0,0,0));self.camera_overview.grid(row=0,column=1,sticky='nsew');self.camera_overview.columnconfigure(0,weight=1);self.camera_overview.rowconfigure(3,weight=1)
        self.camera_overview_title=tk.StringVar(value='전체 지그 · 영상 대기')
        ttk.Label(self.camera_overview,textvariable=self.camera_overview_title,style='Section.TLabel').grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,6))
        self.camera_jig_status=ttk.Treeview(self.camera_overview,columns=('name','status','adoption'),displaycolumns=('name','status'),show='headings',height=4,selectmode='browse')
        self.camera_jig_status.heading('name',text='지그');self.camera_jig_status.heading('status',text='현재 상태')
        self.camera_jig_status.column('name',width=170,minwidth=150);self.camera_jig_status.column('status',width=150,minwidth=140,stretch=False)
        self.camera_jig_status.heading('adoption',text='채택 상태');self.camera_jig_status.column('adoption',width=150,minwidth=140,stretch=False)
        self.camera_jig_status.grid(row=1,column=0,sticky='ew');scroll=AutoScrollbar(self.camera_overview,command=self.camera_jig_status.yview);scroll.grid(row=1,column=1,sticky='ns');self.camera_jig_status.configure(yscrollcommand=scroll.set)
        self.camera_jig_status.tag_configure('detected',foreground='#12634D');self.camera_jig_status.tag_configure('missing',foreground='#8A3D15');self.camera_jig_status.tag_configure('waiting',foreground=COLORS['muted'])
        self.camera_jig_status.bind('<Double-1>',self.open_camera_overview_jig)
        self.camera_overview_details={};self.camera_overview_detail=tk.StringVar(value='행 선택: 상세 상태 · 더블클릭: 범위 편집')
        detail=tk.Label(self.camera_overview,textvariable=self.camera_overview_detail,font=('Noto Sans CJK KR',10),fg=COLORS['muted'],bg='white',height=5,width=1,anchor='nw',justify='left')
        detail.grid(row=2,column=0,columnspan=2,sticky='ew',pady=(8,0));detail.bind('<Configure>',lambda e:detail.configure(wraplength=max(1,e.width)))
        self.camera_jig_status.bind('<<TreeviewSelect>>',self.show_camera_overview_detail)
        self.camera_overview.grid_remove()
        self.camera_status=tk.StringVar(value='카메라 연결 대기');self.camera_status_label=tk.Label(card,textvariable=self.camera_status,font=('Noto Sans CJK KR',11),fg=COLORS['muted'],bg='white',height=3,width=1,anchor='w',justify='left',wraplength=1000);self.camera_status_label.grid(row=2,column=0,sticky='ew',pady=10)
        self.camera_status_label.bind('<Configure>',lambda e:self.camera_status_label.configure(wraplength=max(200,e.width)))
        self.camera_adoption_text=tk.StringVar(value='채택 상태 · 미채택')
        self.camera_adoption_label=ttk.Label(card,textvariable=self.camera_adoption_text,style='Small.TLabel');self.camera_adoption_label.grid(row=3,column=0,sticky='w',pady=(0,5))
        self.refresh_adoption_display()
        sx,sy=self.mesh_profile['size_mm'][:2];ratio=max(sx,sy)/min(sx,sy)
        self.camera_profile_text=tk.StringVar()
        ttk.Label(card,textvariable=self.camera_profile_text,style='Small.TLabel').grid(row=4,column=0,sticky='w')
        saved=ttk.Frame(card,style='Card.TFrame');saved.grid(row=5,column=0,sticky='ew',pady=(6,0));saved.columnconfigure(1,weight=1)
        ttk.Label(saved,text='영상 저장 위치',style='Small.TLabel').grid(row=0,column=0,padx=(0,8))
        self.camera_save_path=tk.StringVar(value=str((self.data_dir/'captures').resolve()))
        self.camera_save_path_entry=ttk.Entry(saved,textvariable=self.camera_save_path,state='readonly');self.camera_save_path_entry.grid(row=0,column=1,sticky='ew')
        self.change_camera_view(persist=False)
    def build_devices(self,parent):
        from .dual_monitor import DualMonitor
        self.device_message=tk.StringVar(master=self.root,value='장비 알림 없음')
        self.dual_monitor=DualMonitor(parent,self);self.dual_monitor.pack(fill='both',expand=True)
    def build_model(self,parent):
        parent.columnconfigure(1,weight=1);parent.rowconfigure(0,weight=1)
        card=self.card(parent);card.grid(row=0,column=0,sticky='nsew',padx=(0,12))
        preview=self.card(parent);preview.grid(row=0,column=1,sticky='nsew');preview.columnconfigure(0,weight=1);preview.rowconfigure(1,weight=1)
        ttk.Label(preview,text='3D 자세 확인',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.model_canvas=tk.Canvas(preview,bg=COLORS['viewport'],highlightthickness=0,width=300)
        self.model_canvas.grid(row=1,column=0,sticky='nsew',pady=12)
        self.model_canvas.bind('<Configure>',lambda e:self.fit_image(self.model_canvas,self.last_rgb) if self.last_rgb is not None and (self.mode!='live' or self.latest and self.latest.fresh()) else None)
        self.model_canvas.bind('<ButtonPress-1>',lambda e:setattr(self,'drag_start',(e.x,e.y,self.view)))
        self.model_canvas.bind('<B1-Motion>',self.rotate_view);self.model_canvas.bind('<Button-4>',lambda e:self.zoom(.9));self.model_canvas.bind('<Button-5>',lambda e:self.zoom(1.1))
        self.bind_pan(self.model_canvas)
        ttk.Label(preview,textvariable=self.tcp_label,style='Small.TLabel').grid(row=3,column=0,sticky='w')
        controls=ttk.Frame(preview);controls.grid(row=4,column=0,sticky='ew',pady=10)
        self.button(controls,'실물값 보기',lambda:self.set_mode('live')).pack(side='left',padx=3)
        self.button(controls,'스텝 자세',lambda:self.set_mode('target')).pack(side='left',padx=3)
        self.button(controls,'시점 복원',self.reset_view).pack(side='left',padx=3)
        ttk.Label(preview,text='드래그: 회전 · 오른쪽/가운데: 이동 · 휠: 확대\n파랑: 베이스 원점 (0, 0, 0) · 주황: TCP\n원: 팔레트·지그 중심',style='Small.TLabel',wraplength=500).grid(row=5,column=0,sticky='w')
        ttk.Label(card,text='관절 보정 · 계산에 적용',style='Section.TLabel').pack(anchor='w')
        ttk.Label(card,text='3D·TCP·지그 보정 계산에 함께 적용됩니다. 지그 보정 실행 시 목표 틱이 달라질 수 있습니다.\n모터 영점·저장된 스텝 틱은 유지됩니다. 토크를 끈 뒤 적용하세요.',style='Muted.TLabel',wraplength=450).pack(anchor='w',pady=12)
        form=ttk.Frame(card,style='Card.TFrame');form.pack(anchor='w');self.trim_vars={};self.trim_controls=[]
        for i,(n,label) in enumerate(zip(JOINTS,LABELS)):
            ttk.Label(form,text=label+'  모델 보정 Δ틱').grid(row=i,column=0,sticky='w',pady=6)
            v=tk.IntVar(value=self.reference.trims[n]);self.trim_vars[n]=v
            spin=ttk.Spinbox(form,from_=-512,to=512,textvariable=v,width=8);spin.grid(row=i,column=1,padx=12);self.trim_controls.append(spin)
            scale=ttk.Scale(form,from_=-512,to=512,variable=v,length=160);scale.grid(row=i,column=2);self.trim_controls.append(scale)
        self.table_z=tk.StringVar(value=f'{floor_adjustment(self.profile):g}')
        ttk.Label(form,text='로봇팔 높이 보정 (mm)').grid(row=6,column=0,sticky='w',pady=8);ttk.Entry(form,textvariable=self.table_z,width=10).grid(row=6,column=1)
        ttk.Label(form,text='직접 설치 0mm · 마운트 결합 5mm',style='Small.TLabel').grid(row=6,column=2,sticky='w',padx=(8,0))
        ttk.Label(form,text='원점이 높아질수록 +값입니다. 직접 설치 0mm, 상단 마운트 결합 +5mm가 기준입니다.\n+5mm를 적용하면 팔 기준 작업대 높이는 5mm 낮아집니다. 팔 번호가 아니라 설치 방식에 맞춰 설정하세요.',style='Small.TLabel',wraplength=420).grid(row=7,column=0,columnspan=3,sticky='w',pady=(0,6))
        row=ttk.Frame(card,style='Card.TFrame');row.pack(anchor='w',pady=20)
        self.button(row,'보정 적용·저장',self.save_model,True).pack(side='left',padx=(0,8));self.button(row,'보정 0으로 복원',self.reset_model).pack(side='left')
        self.angle_mode_note=tk.StringVar();ttk.Label(card,textvariable=self.angle_mode_note,wraplength=420,style='Muted.TLabel').pack(anchor='w')
        self.update_angle_controls()
    def update_angle_controls(self):
        active=bool(self.calibration.angle_mapping)
        for w in self.trim_controls:w.state(['disabled'] if active else ['!disabled'])
        for n,v in self.trim_vars.items():v.set(self.reference.trims[n])
        self.angle_mode_note.set('3점 각도 보정 적용 중 · Δ틱 보정은 중복 적용하지 않습니다.' if active else '3점 보정 없음 · 기존 기본 비율과 Δ틱 보정을 사용합니다.')
    def save_model(self):
        self.settings.require_calculation_idle()
        if self.pending_execution or self.playing or self.pose_latch.frozen or getattr(self.session,'state','READ_ONLY')!='READ_ONLY':raise ValueError('먼저 토크를 해제한 뒤 모델·계산 보정을 적용하세요.')
        trims={n:int(round(float(v.get()))) for n,v in self.trim_vars.items()};z=floor_from_adjustment(self.table_z.get());seconds=self.pose_latch.seconds
        if not math.isfinite(seconds) or not 0<=seconds<=60:raise ValueError('위치 유지 시간은 0~60초입니다.')
        checked_reference=deepcopy(self.reference);checked_reference.set_trims(trims)
        profile=profile_with_arm_height(self.profile,self.table_z.get())
        profile['model_reference']['trim_ticks']=dict(trims)
        if self.remote_mode and self.remote:
            if self.remote.error:raise ValueError('Pi 통신을 복구한 뒤 모델·계산 보정을 적용하세요.')
            from .remote_config import configuration_bundle
            bundle=configuration_bundle(self)
            for key in ('table_z_mm','world_from_base','extrinsics','model_reference'):
                if key in profile:bundle['profile'][key]=deepcopy(profile[key])
            bundle['trim_ticks']=trims
            self.remote.sync_bundle(bundle)
        self.reference.set_trims(trims);self.profile=profile;self.catalog.floor_profile=deepcopy(self.profile);self.pose_latch.configure(seconds);self.detector.seconds=seconds
        for latch in self.detector.latches.values():latch.configure(seconds)
        atomic_json(self.adjustments_path,{'calibration_sha256':self.calibration.sha256,'trim_ticks':trims,'table_z_mm':z,'height_reference':'arm_origin_up','hold_seconds':seconds,'verified':False})
        self.persist_configuration()
        if self.settings.active_profile_key in self.settings.library.items:
            self.settings.library.save(self.profile['name'],self.profile,self.settings.active_profile_key)
        self.update_selected_arm_preview()
        self.invalidate_jig_measurements()
        self.render_context+=1;self.last_render=None;self.last_fk_angles=None;self.notice('모델·계산 보정 저장 · 지그 보정 계산에 적용됩니다. 영점·저장 틱은 유지합니다.')
    def reset_model(self):
        for v in self.trim_vars.values():v.set(0)
        self.table_z.set('0');self.save_model()
    def save_camera_hold(self):
        try:seconds,acquisition,attempts=map(float,(self.hold_seconds.get(),self.acquisition_seconds.get(),self.measurement_attempts.get()))
        except ValueError:raise ValueError('시간은 초 단위 숫자로 입력하세요.') from None
        from .jig_consensus import checked_seconds,checked_attempts,HOLD_RANGE,ACQUISITION_RANGE
        checked_seconds(seconds,HOLD_RANGE,'실행 기준 유지 시간')
        checked_seconds(acquisition,ACQUISITION_RANGE,'지그 획득 시간')
        attempts=checked_attempts(attempts)
        with self.detector.lock:
            if self.pending_execution or self.camera_task or self.safe_entry or self.playing or self.detector.frozen or self.pose_latch.frozen:
                raise ValueError('측정·실행이 끝난 뒤 지그 시간을 적용하세요.')
            settings=read_json(self.adjustments_path) if self.adjustments_path.exists() else {}
            settings.update(calibration_sha256=self.calibration.sha256,trim_ticks=self.reference.trims,
                            table_z_mm=self.profile.get('table_z_mm',-7.4),hold_seconds=seconds)
            atomic_json(self.adjustments_path,settings)
            self.preferences.pop('jig_accept_seconds',None);self.preferences.pop('jig_measurement_timeout_seconds',None)
            self.preferences.update(jig_acquisition_seconds=acquisition,jig_measurement_attempts=attempts)
            atomic_json(self.data_dir/'preferences.json',self.preferences)
            self.measurement_attempts_limit=attempts;self.detector.seconds=seconds;self.detector.acquisition_seconds=acquisition;self.detector.attempts_limit=attempts
            for latch in self.detector.latches.values():latch.configure(seconds,acquisition_seconds=acquisition,attempts_limit=attempts)
            self.invalidate_jig_measurements()
        self.notice(f'획득 {acquisition:g}초 동안 모든 새 관측 수집 · 최대 {attempts}회 시도 · 위치 유지 {seconds:g}초 적용')

    def show_page(self,page,*,internal=False):
        if not internal and page!=self.page:
            if (self.camera_task or {}).get('kind')=='follow':
                self.camera_task=None;self.notice('화면 이동 · 리더 따라가기 준비를 취소했습니다.')
            self.live_adjust.stop()
        previous=self.page
        self.cancel_jig_key()
        self.guidance.set({'teach':'스텝 선택 → 지그 따라가기 선택 → 자세 편집 → 저장 · 실행', 'camera':'지그 검출에서 위치 채택 · 부품·조립 검사에서 윗면 형상과 조립 단계 확인', 'devices':'현재 모터값과 중단 이유를 확인합니다. 연결만으로 토크가 켜지지는 않습니다.', 'model':'3D 표시와 TCP·지그 보정 계산에 함께 적용됩니다. 화면만 바꾸는 설정이 아닙니다.','settings':'로봇·영점, 지그, 카메라와 TCP 설정을 한곳에서 관리합니다.'}[page])
        self.page=page;self.last_render=None;self.pages[page].tkraise();self.page_title.set({'teach':'티칭 워크스페이스','camera':'상단 카메라','devices':'장비 모니터링','model':'모델·계산 보정','settings':'설정 · 안내'}[page])
        for k,b in self.nav_buttons.items():b.configure(bg='#245465' if k==page else COLORS['nav'],fg='white' if k==page else '#d0e1e8',text=('● ' if k==page else '   ')+{'teach':'티칭','camera':'상단 카메라','devices':'장비 모니터링','model':'모델·계산 보정','settings':'설정 · 안내'}[k])
        self.camera_page_changed(previous)
        if page=='teach':self.guidance.set('완제품·안착 검사·완료 알림·실행 설정 · 저장 → Pi 에피소드 내보내기' if self.episode_adjusting() else '자세 편집 → 새 스텝 추가 / 선택 스텝 수정 → 경로 미리보기 → 실물 실행')
    def apply_target(self,ticks,*,preview=False):
        if not preview:
            self.preview_jig_references=None;self.measured_target_active=False
            if self.taught_jig_display is not None:self.taught_jig_display['target_active']=False
        self.preview_editor=1
        self.target=self.calibration.ticks(ticks);self.set_guard=True
        try:
            for n,v in ticks.items():self.tick_vars[n].set(str(v));self.sliders[n].set(v)
        finally:self.set_guard=False
    def input_tick(self,name):
        if self.set_guard:return
        try:
            value=int(self.tick_vars[name].get())
            if self.live_adjust.edit(self,name,value):return
            new={**self.target,name:value};self.calibration.ticks(new)
        except ValueError as exc:self.tick_vars[name].set(str(self.target[name]));self.notice(str(exc),True);return
        self.stop_preview(quiet=True);self.apply_target(new);self.set_mode('target')
    def slider_tick(self,name,value):
        if self.set_guard:return
        if self.live_adjust.owner is self:return
        self.live_adjust.stop()
        self.stop_preview(quiet=True);self.apply_target({**self.target,name:round(float(value))});self.set_mode('target')
    def set_mode(self,mode):
        if mode==self.mode:return
        # Invalidate late frames without exposing an empty canvas between sources.
        self.render_context+=1
        fresh=self.live_preview_sample() is not None
        if self.last_rgb is not None and (mode!='live' or fresh):
            self.pending_render_context=self.render_context
            self.preview_caption.set('3D 자세 갱신 중 · 이전 화면 표시')
        else:
            self.pending_render_context=None;self.last_rgb=None
            self.preview_canvas.delete('image');self.model_canvas.delete('image')
        if mode=='live':self.live_was_fresh=fresh
        self.mode=mode;self.live_btn.configure(style='Primary.TButton' if mode=='live' else 'TButton');self.target_btn.configure(style='Primary.TButton' if mode=='target' else 'TButton')
        self.last_render=None
    def toggle_connection(self):
        if calibration_pending(self.profile):
            self.show_page('settings');self.settings.tabs.select(self.settings.pages['calibration']);return
        self.live_adjust.stop()
        if self.session and self.session.running:
            self.session.close()
            if self.leader_session:self.leader_session.close()
            self.connection.set('연결 해제 중');return
        self.connect_devices()
    def connect_devices(self):
        if self.camera_only:raise ValueError('카메라 전용 실행에서는 모터를 연결하지 않습니다.')
        # Startup may adopt an existing Pi session. Never toggle it off or arm it.
        require_calibrated(self.profile)
        connected=bool(self.session and self.session.running)
        if self.remote_mode and not connected:self.require_remote().sync(self)
        leader_config=None;leader_cal=None
        if self.profile.get('mode')=='leader':
            from .domain import Calibration
            leader_config=self.profile.get('leader') or {}
            if not leader_config.get('port') or not leader_config.get('calibration_file'):
                raise ValueError('리더 설정이 필요합니다. 설정·안내 → 3점 각도 보정에서 리더를 보정하거나 로봇 탭에서 리더 포트·JSON을 등록하세요.')
            if not self.remote_mode and Path(leader_config['port']).resolve()==Path(self.profile['port']).resolve():raise ValueError('리더와 팔로워 포트는 달라야 합니다.')
            leader_cal=Calibration(self.data_dir/leader_config['calibration_file'])
            if leader_config.get('angle_mapping_sha256') and (not leader_cal.angle_mapping or leader_cal.angle_mapping.sha256!=leader_config['angle_mapping_sha256']):raise ValueError('리더의 3점 각도 보정 파일이 없거나 변경되었습니다.')
        if connected:
            if leader_config and not (self.leader_session and self.leader_session.running):self.start_leader_connection(leader_config,leader_cal)
            return
        path=self.data_dir/'diagnostics'/f'session-{time.time_ns()}.json'
        factory=MotionSession
        if self.remote_mode:
            from .remote_client import RemoteMotionSession
            factory=lambda *args,**kw:RemoteMotionSession(self.remote,*args,**kw)
        self.latest=None;self.session=DemoSession(self.calibration,self.reference.middle) if self.profile.get('mode')=='demo' else factory(self.profile['port'],self.calibration,audit_path=path,rate_ticks_s=SPEED_PRESETS[self.motion_speed_choice.get()],grip_contact_load=self.profile.get('grip_contact_load',80),follow_start_rate_ticks_s=self.profile.get('follow_start_rate_ticks_s',400.))
        self.session.set_speed(SPEED_PRESETS[self.motion_speed_choice.get()]);self.session.start()
        if leader_config:self.start_leader_connection(leader_config,leader_cal)
        self.connection.set('Pi 모터 연결 중' if self.remote_mode else '모터 연결 중')
    def start_leader_connection(self,config,calibration):
        if self.camera_only:raise ValueError('카메라 전용 실행에서는 리더를 연결하지 않습니다.')
        path=self.data_dir/'diagnostics'/f'leader-session-{time.time_ns()}.json'
        if self.remote_mode:
            from .remote_client import RemoteLeaderSession
            self.leader_session=RemoteLeaderSession(self.require_remote(),config['port'],calibration,role='leader',audit_path=path)
        else:self.leader_session=LeaderAssistSession(config['port'],calibration,role='leader',audit_path=path)
        self.leader_session.poll_seconds=.02
        self.bind_leader_connection(calibration);self.leader_session.start()
    def bind_leader_connection(self,calibration=None):
        leader=self.leader_session;session=self.session
        if leader is None or session is None:return False
        calibration=calibration or getattr(leader,'calibration',None) or session.leader_calibration
        if calibration is None:raise ValueError('리더 보정 정보를 불러오지 못했습니다. 리더 포트와 보정 JSON을 확인하세요.')
        session.leader_calibration=calibration
        session.leader_provider=lambda:leader.latest if leader.running else None
        return True
    def prepare_leader_follow(self):
        if self.profile.get('mode')!='leader':raise ValueError('로봇 설정에서 리더 + 팔로워 모드를 선택하세요.')
        leader=self.leader_session
        if not leader or not leader.running:
            from .domain import Calibration
            config=self.profile.get('leader') or {}
            if not config.get('port') or not config.get('calibration_file'):raise ValueError('리더 포트와 보정 JSON을 등록하세요.')
            if not self.remote_mode and Path(config['port']).resolve()==Path(self.profile['port']).resolve():raise ValueError('리더와 팔로워 포트는 달라야 합니다.')
            cal=Calibration(self.data_dir/config['calibration_file'])
            if config.get('angle_mapping_sha256') and (not cal.angle_mapping or cal.angle_mapping.sha256!=config['angle_mapping_sha256']):raise ValueError('리더의 3점 각도 보정 파일이 없거나 변경되었습니다.')
            self.start_leader_connection(config,cal)
            self.notice('리더 읽기 연결 중 · 현재값 수신 후 따라가기를 시작합니다.');return False
        sample=leader.latest
        if not sample or not sample.fresh():raise ValueError('리더 현재값 수신 대기 · 잠시 후 다시 눌러 주세요.')
        if not sample.calibration_matches:raise ValueError('리더 모터 영점·범위와 선택한 보정 JSON이 다릅니다. 리더 보정 파일을 확인하세요.')
        self.bind_leader_connection()
        leader_target(sample.ticks,self.session.leader_calibration,self.calibration)
        return True
    def poll_leader_status(self):
        leader=self.leader_session
        if not leader:return
        active=(getattr(self.session,'state',None)=='FOLLOW' or
                (self.camera_task or {}).get('kind')=='follow')
        def report(text,error=False):
            # Idle leader diagnostics belong in monitoring, not teaching status.
            if active:self.device_notice(text,error)
            else:self.device_message.set(text)
        while True:
            try:
                kind,value=leader.events.get_nowait()
                if kind in ('notice','device_notice','error'):report(value,kind=='error')
            except queue.Empty:break
        sample=leader.latest
        issue=leader.error or ('리더 모터 영점·범위와 보정 JSON 불일치' if sample and sample.fresh() and not sample.calibration_matches else None)
        key=(id(leader),issue,active)
        if issue and key!=getattr(self,'last_leader_issue',None):
            report('리더 연결 확인: '+issue+' · 리더 다시 연결 버튼으로 재시도할 수 있습니다.',True)
        self.last_leader_issue=key if issue else None
    def copy_live(self):
        self.live_adjust.stop()
        if not self.latest or not self.latest.fresh():raise ValueError('최신 실물 위치가 없습니다.')
        self.apply_target(self.latest.ticks);self.set_mode('target');self.notice('실물 틱을 스텝 자세로 복사했습니다. 모터에는 명령을 보내지 않습니다.')
    def capture(self):
        require_calibrated(self.profile)
        if self.safe_editing:raise ValueError('안전 자세 설정에서는 실물값을 불러온 뒤 안전 자세 적용을 누르세요.')
        if not self.latest:raise ValueError('팔로워 연결 후 저장하세요.')
        step=self.with_jig(self.store.step(self.latest.ticks,self.step_name.get(),{'kind':'demo'}) if self.latest.role=='demo' else self.store.capture(self.latest,self.step_name.get()))
        self.prepare_completion_edit(step)
        if not self.append_step(step):return
        self.step_name.set(step['name']);self.prune_unused_teaching_references();self.selected=step['id'];self.apply_target(step['ticks']);self.refresh_steps();self.save_episode();self.notice('데모 자세를 구분하여 저장했습니다.' if self.latest.role=='demo' else '실물에서 읽은 6개 틱을 스텝으로 저장했습니다.')
    def commit_target(self):
        require_calibrated(self.profile)
        if self.safe_editing:
            self.add_safe_steps();self.end_safe_edit(restore=False);return
        self.save_edited_step(update=False)
    def update_selected_step(self):self.save_edited_step(update=True)
    def save_edited_step(self,*,update):
        require_calibrated(self.profile)
        if self.safe_editing:raise ValueError('안전 자세 적용 버튼을 사용하세요.')
        index=next((i for i,s in enumerate(self.episode['steps']) if s['id']==self.selected),None)
        if update and (index is None or self.episode['steps'][index].get('safe_boundary')):
            raise ValueError('수정할 일반 스텝을 선택하세요. 안전 자세는 안전 자세 설정에서 변경합니다.')
        ticks={n:int(v.get()) for n,v in self.tick_vars.items()};self.apply_target(ticks)
        original=self.episode['steps'][index] if update else None
        step=self.with_jig(self.store.step(ticks,self.step_name.get()),original=original)
        if original:step={**deepcopy(original),**step,'id':self.selected}
        self.prepare_completion_edit(step)
        if update:
            step['id']=self.selected;self.episode['steps'][index]=step
        elif not self.append_step(step):return
        self.step_name.set(step['name'])
        self.prune_unused_teaching_references()
        self.selected=step['id'];self.refresh_steps();self.save_episode()
        self.notice('선택한 스텝을 수정했습니다.' if update else '새 스텝을 추가했습니다. 기존 스텝은 유지됩니다.')
    def prepare_completion_edit(self,step):
        from .episode_events import events_for,set_event
        self.episode.setdefault('completion_events',events_for(self.episode))
        if getattr(self,'completion_changed',False):set_event(self.episode,step['id'],self.completion_event.get())
        self.completion_changed=False
    def change_motion_speed(self):
        choice=self.motion_speed_choice.get();rate=SPEED_PRESETS[choice]
        try:
            if self.pending_execution or self.safe_entry:raise ValueError('실행이 끝난 뒤 속도를 바꾸세요.')
            if isinstance(self.session,MotionSession):self.session.set_speed(rate)
        except Exception:
            self.motion_speed_choice.set(self.preferences.get('motion_speed','보통'));raise
        self.preferences['motion_speed']=choice;self.save_preferences()
        self.notice(f'다음 실물 이동: {choice} · {rate:g}틱/초 (약 {rate*360/4096:.1f}°/초)')
    def save_episode(self):
        require_calibrated(self.profile)
        self.episode['name']=self.episode_name.get().strip();p=self.store.save(self.episode);self.sync_remote_episode(self.episode);self.preferences['last_episode_id']=self.episode['id'];self.save_preferences();self.refresh_library();self.notice('저장됨 · '+self.episode['name']);return p
    def new_episode(self):
        self.live_adjust.stop()
        self.close_second_editor()
        self.end_safe_edit(restore=False)
        if self.episode['steps']:self.save_episode()
        self.stop_preview(quiet=True);self.episode=self.store.new();self.selected=None;self.episode_name.set(self.episode['name']);self.step_name.set('자세 1');self.follow_jig.set(False);self.update_jig_hint();self.refresh_steps()
    def refresh_library(self):
        self.library_entries=self.store.entries();self.library.delete(0,'end')
        sync=getattr(self,'episode_sync',None);recipes=(sync.snapshot or {}).get('recipes',{}) if sync else {}
        order={v['episode_id']:i for i,v in enumerate(recipes.values())}
        self.library_entries.sort(key=lambda row:(order.get(row[1]['id'],99),row[1]['name']))
        for i,(p,d) in enumerate(self.library_entries):
            sync=getattr(self,'episode_sync',None)
            self.library.insert('end',sync.label(d) if sync else d['name'])
            if d['id']==self.episode['id']:self.library.selection_set(i)
    def load_selected_episode(self,event=None):
        self.live_adjust.stop()
        selection=self.library.curselection()
        if not selection:return
        path=self.library_entries[selection[0]][0]
        def load():
            self.close_second_editor()
            self.end_safe_edit(restore=False)
            self.stop_preview(quiet=True);self.episode=self.store.load(path);self.prune_unused_teaching_references();self.episode_name.set(self.episode['name']);self.selected=None;self.follow_jig.set(False);self.update_jig_hint();self.preferences['last_episode_id']=self.episode['id'];self.save_preferences();self.refresh_steps()
            if self.episode['steps']:
                self.steps.selection_set(self.episode['steps'][0]['id']);self.select_step()
        self.guard(load)
    def refresh_steps(self):
        editor=getattr(self,'second_editor',None)
        if editor and (editor.episode_id!=self.episode['id'] or not any(s['id']==editor.selected for s in self.episode['steps'])):self.close_second_editor()
        self.steps.delete(*self.steps.get_children())
        if self.episode['steps']:self.empty_steps.place_forget()
        else:self.empty_steps.place(relx=.5,y=70,anchor='n')
        for i,s in enumerate(self.episode['steps'],1):self.steps.insert('','end',iid=s['id'],values=(f'{i:02}   '+s['name']+(' · 데모' if s.get('source',{}).get('kind')=='demo' else ''),'지그' if s.get('jig_id') else '고정'))
        if self.selected in self.steps.get_children():self.steps.selection_set(self.selected);self.steps.see(self.selected)
        self.mark_editor_steps()
        self.refresh_jig_comparison()
        self.refresh_episode_policy()
    def select_step(self,event=None):
        chosen=self.steps.selection()
        if not chosen:return
        if chosen[0]==self.selected and (event is not None or self.safe_editing):return
        self.live_adjust.stop()
        self.end_safe_edit(restore=False)
        self.selected=chosen[0];s=next(s for s in self.episode['steps'] if s['id']==self.selected)
        self.stop_preview(quiet=True);self.follow_jig.set(bool(s.get('jig_id')));self.set_step_jig(s.get('jig_id'));self.update_jig_hint();self.step_name.set(s['name']);self.apply_target(s['ticks']);self.set_mode('target')
        self.jig_comparison.select_step(self.selected)
        from .episode_events import event_label
        self.completion_event.set(event_label(self.episode,self.selected));self.completion_changed=False
        if hasattr(self,'episode_adjust_panel'):self.episode_adjust_panel.refresh_selected()
        self.mark_editor_steps()

    def save_comparison_visibility(self,collapsed):
        self.preferences['jig_comparison_collapsed']=bool(collapsed);atomic_json(self.data_dir/'preferences.json',self.preferences)

    def refresh_jig_comparison(self):
        from .jig_comparison import comparison_groups,steps_signature
        key=self.episode['id'];report=self.comparison_reports.get(key)
        if report is None:
            paths=sorted((self.data_dir/'episode_runs'/key).glob('*.json'))
            if paths:
                try:report=read_json(paths[-1]);self.comparison_reports[key]=report
                except (OSError,ValueError):report=None
        if report and report.get('episode_steps_signature')==steps_signature(self.episode['steps']):
            self.jig_comparison.show(report['groups'],self.comparison_caption(report))
        else:self.jig_comparison.show(comparison_groups(self.episode['steps'],self.catalog.items),'실행 전 · 저장된 기준')

    def comparison_caption(self,report):
        mode='미리보기' if report['action']=='preview' else '실물 실행'
        state={'measured':'측정 완료','submitted':'명령 전달','preview':'경로 계산','planned':'실행 준비','blocked':'실행 안 됨'}.get(report['state'],report['state'])
        if report.get('execution_mode')=='taught_ticks':mode='티칭 그대로 실행 · 측정·보정 없음'
        return f"최근 {mode} · {time.strftime('%H:%M:%S',time.localtime(report.get('requested_at') or report['measured_at']))} · {state}"

    def update_live_jig_comparison(self):
        if (self.pending_execution or self.safe_entry or self.playing or self.detector.frozen
                or getattr(self.session,'state','') in ('MOVING','FOLLOW','ACTIVATING')
                or getattr(self.session,'program_active',None) and self.session.program_active.is_set()):return
        if self.jig_comparison.view.get()=='최근 실행':self.refresh_jig_comparison();return
        from .jig_comparison import comparison_groups
        now=time.monotonic();current={};remaining=[]
        results=self.camera_results(now)
        keys={s['jig_id'] for s in self.episode['steps'] if s.get('jig_id')}
        for key in keys:
            result=results.get(key,{}) or {};ref=self.current_jig_reference(key) if result else None
            if ref:
                current[key]=ref
                if not result.get('teaching_held'):remaining.append(float(result.get('hold_remaining_s',0)))
        caption=f"확정값 · {len(current)}/{len(keys)} 감지" if current else '확정값 · 지그 감지 대기'
        if self.teaching_jig_hold_active():caption='자동 갱신 정지 · '+(f'{len(current)}/{len(keys)} 위치 유지' if current else '지그 다시 읽기 필요')
        if remaining:caption+=f" · 유지 {min(remaining):.1f}초"
        self.jig_comparison.show(comparison_groups(self.episode['steps'],self.catalog.items,current),caption,live=True)

    def save_jig_comparison(self,report):
        atomic_json(self.data_dir/'episode_runs'/report['episode_id']/(str(report['id'])+'.json'),report)
        self.comparison_reports[report['episode_id']]=deepcopy(report)
        self.jig_comparison.show(report['groups'],self.comparison_caption(report))
    def move_step(self,direction):
        ids=[s['id'] for s in self.episode['steps']]
        if self.selected not in ids:return
        if self.episode['steps'][ids.index(self.selected)].get('safe_boundary'):return
        i=ids.index(self.selected);j=i+direction
        if 0<=j<len(ids) and not self.episode['steps'][j].get('safe_boundary'):self.episode['steps'][i],self.episode['steps'][j]=self.episode['steps'][j],self.episode['steps'][i];self.refresh_steps();self.save_episode()
    def drop_step(self,event):
        if self.episode_adjusting():return
        source=getattr(self,'drag_step',None);dest=self.steps.identify_row(event.y);ids=[s['id'] for s in self.episode['steps']]
        if source in ids and dest in ids and source!=dest and not any(self.episode['steps'][ids.index(k)].get('safe_boundary') for k in (source,dest)):
            step=self.episode['steps'].pop(ids.index(source));self.episode['steps'].insert(ids.index(dest),step);self.selected=source;self.refresh_steps();self.guard(self.save_episode)
    def delete_step(self):
        ids=[s['id'] for s in self.episode['steps']]
        if self.selected not in ids:return
        index=ids.index(self.selected)
        selected=next((s for s in self.episode['steps'] if s['id']==self.selected),{})
        if selected.get('safe_boundary'):self.episode['steps']=[s for s in self.episode['steps'] if not s.get('safe_boundary')]
        self.episode['steps']=[s for s in self.episode['steps'] if s['id']!=self.selected];self.selected=None
        if 'completion_events' in self.episode:
            ids={s['id'] for s in self.episode['steps']};self.episode['completion_events']={k:v for k,v in self.episode['completion_events'].items() if v in ids}
        self.prune_unused_teaching_references();self.follow_jig.set(False);self.update_jig_hint();self.refresh_steps()
        if self.episode['steps']:
            key=self.episode['steps'][min(index,len(self.episode['steps'])-1)]['id']
            self.steps.selection_set(key);self.steps.focus(key);self.steps.see(key);self.select_step()
        self.save_episode()
    def prune_unused_teaching_references(self):
        used={s['jig_id'] for s in self.episode['steps'] if s.get('jig_id')}
        refs=self.episode.setdefault('jig_references',{})
        for key in list(refs):
            if key not in used:refs.pop(key)
        # Register a basis only after a following step has actually been added.
        # Reordering steps or deleting the first one must not change a used basis.
        for step in sorted(self.episode['steps'],key=lambda s:s.get('saved_at',0)):
            if step.get('jig_id'):refs.setdefault(step['jig_id'],deepcopy(step['jig_reference']))
    def current_jig_reference(self,jig_id=None):
        jig_id=jig_id or self.selected_jig_id()
        result=self.camera_results().get(jig_id)
        return self.jig_result_reference(jig_id,result)
    def held_jig_reference(self,jig_id):
        return self.jig_result_reference(jig_id,self.teaching_jig_results.get(jig_id))
    def jig_result_reference(self,jig_id,result):
        if not result or not result.get('selected'):return None
        if not result.get('teaching_held') and result.get('pose_measured_at',0)<getattr(self,'measurement_valid_after',0.):return None
        metric=result['selected'].get('metric')
        if not metric:return None
        return {'pose':[*metric['center_xy_mm'],metric['yaw_deg']], 'symmetry_deg':metric.get('symmetry_deg',90),
                'stl_sha256':self.catalog.mesh(jig_id)['sha256'],
                **({'mesh_yaw_offset_deg':metric['mesh_yaw_offset_deg']} if metric.get('mesh_yaw_offset_deg') else {})}
    def ensure_teaching_reference(self,jig_id=None):
        jig_id=jig_id or self.selected_jig_id()
        steps=[s for s in self.episode['steps'] if s.get('jig_id')==jig_id]
        if not steps:return None
        return self.episode.get('jig_references',{}).get(jig_id) or min(steps,key=lambda s:s.get('saved_at',0))['jig_reference']
    def new_step_jig_reference(self,jig_id=None):
        jig_id=jig_id or self.selected_jig_id()
        if self.jig_updates_paused:
            # A paused session owns its confirmed pose. Never silently fall
            # back to an older episode basis while the new read is pending.
            task=self.camera_task
            if task and task['kind'] in ('jig','follow') and task.get('jig')==jig_id and not task.get('captured'):return None
            return deepcopy(self.held_jig_reference(jig_id))
        steps=[step for step in self.episode['steps'] if step.get('jig_id')==jig_id]
        if steps:return deepcopy(max(steps,key=lambda step:step.get('saved_at',0))['jig_reference'])
        return deepcopy(self.current_jig_reference(jig_id))
    def update_jig_hint(self):
        app=getattr(self,'app',self);live=app.live_adjust
        if live.owner is self and live.identity(self)!=live.context:live.stop()
        if not self.follow_jig.get():self.jig_hint.set('보정 안 함 · 저장한 모터 틱 그대로 실행');return
        selected=next((s for s in self.episode['steps'] if s['id']==self.selected),{})
        saved=selected.get('jig_reference') if selected.get('jig_id')==self.selected_jig_id() else None
        new_reference=app.new_step_jig_reference(self.selected_jig_id())
        reference=saved or new_reference
        if not reference:self.jig_hint.set('새 스텝 기준\n첫 지그 스텝 저장 시 위치·각도 확정');return
        x,y=display_position(reference['pose'][:2]);angle=display_heading(reference['pose'][2],reference['symmetry_deg'])
        title='저장 초기 기준' if saved else '새 스텝 기준 · 갱신 정지' if self.teaching_jig_hold_active() else '새 스텝 기준'
        text=f'{title}\nX→{x:.1f} · Y↑{y:.1f} mm · {angle:.1f}°'
        if saved and new_reference!=saved:
            if new_reference:
                nx,ny=display_position(new_reference['pose'][:2]);na=display_heading(new_reference['pose'][2],new_reference['symmetry_deg'])
                text+=f'\n새 스텝 기준 X→{nx:.1f} · Y↑{ny:.1f} · {na:.1f}°'
            else:text+='\n새 스텝 기준: 지그 다시 읽기 필요'
        self.jig_hint.set(text)
    def with_jig(self,step,original=None):
        if self.follow_jig.get():
            # New steps use the paused teaching session; an explicit edit or
            # move of an existing step retains that step's own basis.
            jig_id=self.selected_jig_id()
            reference=(original.get('jig_reference') if original and original.get('jig_id')==jig_id else self.new_step_jig_reference(jig_id))
            if not reference:raise ValueError('지그 따라가기: 지그 다시 읽기로 먼저 위치를 확인하세요.')
            step.update(jig_id=jig_id,jig_reference=deepcopy(reference))
        return step
    def prepare_execution(self,action,steps,*,apply_jig=True):
        if not self.live_adjust.dispatching:self.live_adjust.stop()
        validation=deepcopy(steps)
        if validation and validation[0].get('safe_boundary')!='start':
            for step in validation:step.pop('safe_boundary',None)
        subset={**self.episode,'steps':validation}
        if 'completion_events' in subset:
            ids={step['id'] for step in validation}
            subset['completion_events']={kind:key for kind,key in subset['completion_events'].items() if key in ids}
        self.store.validate(subset)
        if not steps:raise ValueError('먼저 스텝을 저장하세요.')
        if self.pending_execution or self.camera_task:raise ValueError('카메라 측정 중입니다. 정지로 취소할 수 있습니다.')
        if isinstance(self.session,MotionSession) and self.session.program_active.is_set():raise ValueError('현재 실행이 끝난 후 다시 실행하세요.')
        if self.remote_plan_job:raise ValueError('Pi에서 경로를 계산 중입니다.')
        self.stop_preview(quiet=True)
        ids={s['jig_id'] for s in steps if s.get('jig_id')} if apply_jig else set()
        from .episode_inspection import inspection_jig_ids,startup_steps
        inspection_ids=inspection_jig_ids([*steps,*(startup_steps(self.episode) if action=='startup' else [])]) if action in ('startup','play') else set()
        ids |= inspection_ids
        self.move_uses_held_jig=bool((action=='move' or action=='preview' and self.jig_updates_paused) and apply_jig and ids)
        held={}
        if self.move_uses_held_jig:
            if not self.jig_updates_paused:
                results=self.camera_results();self.teaching_jig_results.clear()
                self.remember_teaching_jigs(results,since=getattr(self,'measurement_valid_after',0.))
                self.jig_updates_paused=True
            self.update_jig_pause_button();self.update_jig_hint();self.last_render=None
            current={key:self.held_jig_reference(key) for key in ids}
            held={key:value for key,value in current.items() if value}
            self.update_camera_mode()
            if all(current.values()):
                if self.page!='camera':self.suspend_camera()
                self.begin_execution(action,steps,current);return
            # A different jig needs its own first measurement, never another jig's pose.
            missing=next(key for key in sorted(ids) if current[key] is None)
            if self.active_jig!=missing:self.select_camera_jig(missing)
        if ids and (apply_jig or inspection_ids):
            started=time.monotonic();self.refresh_jig_reference()
            from .jig_comparison import comparison_groups
            self.jig_comparison.show(comparison_groups(steps,self.catalog.items),'실행용 지그 측정 중')
            self.pending_execution={'action':action,'steps':deepcopy(steps),'jig_ids':sorted(ids),'apply_jig':apply_jig,'started':started,'budget_started':time.monotonic(),'page':self.page,'hold_for_moves':self.move_uses_held_jig,'held_references':deepcopy(held)}
            try:self.start_camera()
            except Exception:
                self.pending_execution=None;self.suspend_camera();raise
            self.show_page('camera',internal=True);self.notice('실행용 지그 위치 읽기 · 감지되면 돌아와서 이어서 실행합니다.')
        elif apply_jig:self.begin_execution(action,steps,None)
        else:self.begin_execution(action,steps,None,apply_jig=False)
    def begin_execution(self,action,steps,current,*,apply_jig=True,_remote_plan=None):
        require_calibrated(self.profile)
        if action=='startup':
            if not self.startup_inspection(steps,apply_jig=apply_jig,current=current):self.continue_episode(steps,apply_jig=apply_jig,current=current)
            return
        if self.remote_mode and apply_jig and _remote_plan is None and any(s.get('jig_id') for s in steps):
            link=self.require_remote()
            if self.remote_plan_job:raise ValueError('Pi에서 경로를 계산 중입니다.')
            self.set_pose_frozen(True)
            request=(action,deepcopy(steps),deepcopy(current))
            self.remote_plan_job=(self.settings.pool.submit(link.rpc,'plan',{'steps':request[1],'current':request[2]},30.),request,self.episode['id'])
            self.notice('Pi에서 지그 보정 경로 계산 중');return
        from .geometry import corrected_plan
        from .jig_comparison import comparison_groups,steps_signature
        groups=comparison_groups(steps,self.catalog.items,current if apply_jig else None);report=None
        if not groups:self.jig_comparison.show(comparison_groups(self.episode['steps'],self.catalog.items),'이번 실행: 고정 스텝 · 지그 보정 없음')
        if groups or not apply_jig:
            report={'schema':1,'id':time.time_ns(),'episode_id':self.episode['id'],'episode_name':self.episode['name'],
                    'episode_steps_signature':steps_signature(self.episode['steps']),'action':action,'state':'measured' if apply_jig else 'planned',
                    'execution_mode':'jig_correction' if apply_jig else 'taught_ticks','requested_at':time.time(),
                    'measured_at':time.time() if apply_jig else None,'groups':groups,'steps':deepcopy(steps),'calibration_sha256':self.calibration.sha256,
                    'coordinate_frame':'base_link','display_xy_rule':'X=-base_Y, Y=base_X',
                    'jig_position_mode':'held_for_step_moves' if self.move_uses_held_jig else 'execution_measurement'}
            self.save_jig_comparison(report)
        try:
            if apply_jig:
                plan=_remote_plan if _remote_plan is not None else corrected_plan(self.kin,steps,current,lambda key:self.catalog.mesh(key)['sha256'])
            else:
                plan=[{'ticks':self.calibration.ticks(s['ticks']),'corrected':False} for s in steps]
        except Exception as exc:
            if report:report.update(state='blocked',error=str(exc));self.save_jig_comparison(report)
            raise
        self.last_plan=plan;targets=[p['ticks'] for p in plan]
        if action=='preview':
            self.preview_jig_references=deepcopy(current) if current else None
            self.transport.begin(self.target,targets,rate=SPEED_PRESETS[self.motion_speed_choice.get()]);self.timeline.configure(to=self.transport.duration);self.play_targets=targets;self.set_mode('target');self.play_index=0;self.play_start=time.monotonic();self.play_from=self.target.copy();self.playing=True;self.set_pose_frozen(True)
            self.notice('보정 경로 미리보기' if any(p['corrected'] for p in plan) else '고정 틱 경로 미리보기')
        else:
            if isinstance(self.session,MotionSession):self.session.heartbeat=time.monotonic()
            try:
                from .episode_inspection import has_inspections
                if action=='play' and has_inspections({'steps':steps}):
                    from .episode_execution import InspectedExecution
                    self.inspection_run=InspectedExecution(self,steps,targets,fixed_jigs=current);request_id=self.inspection_run.dispatch()
                else:request_id=self.motion_request(action,targets)
            except Exception as exc:
                if self.inspection_run:self.inspection_run.cancel(str(exc),hold=True)
                if report:report.update(state='blocked',error=str(exc));self.save_jig_comparison(report)
                raise
            if action=='move':self.live_adjust.submitted(request_id)
            if apply_jig and current:
                self.measured_jig_display=deepcopy(current);self.measured_target_active=True
            if report:report['request_id']=request_id if type(request_id) is int else None
        if report:
            report.update(state='preview' if action=='preview' else 'submitted',plan=deepcopy(plan))
            # A logging error after dispatch must not be reported as a motion failure.
            try:self.save_jig_comparison(report)
            except OSError as exc:self.notice('지그 비교 기록 저장 실패: '+str(exc),True)
        if not apply_jig:
            self.taught_jig_display={'steps':deepcopy(steps),'request_id':report.get('request_id') if report else None}
            self.last_render=None;self.render_context+=1
        if any(p['corrected'] for p in plan):
            error=max(p.get('position_error_mm',0) for p in plan)
            tilt=max(p.get('tilt_error_deg',0) for p in plan)
            margins=[v for p in plan for v in p.get('joint_margins_deg',{}).values()]
            reserve=f' · 최소 관절 여유 {min(margins):.1f}°' if margins else ''
            low={n for p in plan for n in p.get('low_margin_joints',[])}
            if low:reserve+=' · 3° 미확보: '+', '.join(label for n,label in zip(JOINTS,LABELS) if n in low)
            plane=max((p.get('taught_plane_tilt_deg',0.) for p in plan),default=0.)
            posture_status=' · 위치·관절 범위 우선으로 기울기 차이 남음' if plane>1. else ''
            self.notice(f'지그 보정 · TCP 오차 최대 {error:.2f}mm · 티칭 기울기 차이 {tilt:.1f}° · 수평 기준면 변화 {plane:.1f}°'+reserve+posture_status+' · 실물 대조 필요')
        elif not apply_jig:self.notice('티칭 그대로 실행 · 저장된 모든 스텝의 모터 틱 사용'+(' · 안착 검사는 확정 지그 위치 기준' if any('inspection' in step for step in steps) else ' · 카메라 측정·지그 보정 없음'))
    def check_pending_execution(self):
        if self.camera_waiting_for_arm:return
        pending=self.pending_execution
        if not pending:return
        ids=set(pending.get('jig_ids',{s['jig_id'] for s in pending['steps'] if s.get('jig_id')}))
        held=pending.get('held_references',{});needed=ids-held.keys()
        current={**held,**{key:self.current_jig_reference(key) for key in needed}}
        result=self.camera.observation[1] if self.camera and self.camera.observation else None
        results=result.get('by_jig',{self.active_jig:result}) if result else {}
        ready=all(self.measurement_within_attempts(results.get(k,{})) and current[k] and results.get(k,{}).get('pose_measured_at') is not None and results[k]['pose_measured_at']>=pending['started'] for k in needed)
        if current and ready:
            if pending.get('hold_for_moves'):
                self.remember_teaching_jigs({key:results[key] for key in needed},since=pending['started'])
            self.pending_execution=None;self.set_pose_frozen(True);self.show_page(pending['page'],internal=True)
            if self.page!='camera':self.suspend_camera()
            try:self.begin_execution(pending['action'],pending['steps'],current,apply_jig=pending.get('apply_jig',True))
            except Exception as exc:self.set_pose_frozen(False);self.notice(str(exc),True)
        elif failure:=self.measurement_failure(pending,time.monotonic(),needed):
            self.pending_execution=None;self.show_page(pending['page'],internal=True);self.notice(failure+' · 감지 범위·카메라를 확인하고 다시 실행하세요.',True)
            if self.page!='camera':self.suspend_camera()
            from .jig_comparison import comparison_groups
            self.jig_comparison.show(comparison_groups(pending['steps'],self.catalog.items),'측정 실패 · 이전 측정 사용 안 함')
    def play(self):self.prepare_execution('preview',deepcopy(self.episode['steps']))
    def torque_release_available(self):
        session=self.session
        if not session or not session.running:return False
        if getattr(session,'state',None)=='ACTIVATING':return True
        pending=getattr(session,'command_pending',None)
        if pending and pending.is_set():return True
        sample=session.latest
        confirmed_off=bool(sample and sample.fresh() and
                           all(sample.telemetry.get(n,{}).get('torque')==0 for n in JOINTS))
        return not confirmed_off

    def motion_request(self,action,targets=None):
        shutdown=getattr(self.workspace_manager,'safe_shutdown',None)
        if shutdown and shutdown.busy:
            if action in ('hold','release'):shutdown.cancel()
            else:raise ValueError('안전 종료 진행 중입니다.')
        from .pi_execution import active_execution
        job=active_execution(self)
        if job:
            if action in ('hold','release'):job.cancel(action,self);return
            raise ValueError('Pi 에피소드 실행 중입니다. 정지한 뒤 조작하세요.')
        if self.camera_only:raise ValueError('카메라 전용 실행에서는 모터 명령을 보내지 않습니다.')
        if self.inspection_run and self.inspection_run.busy and not self.inspection_run.dispatching and action not in ('hold','release'):
            raise ValueError('안착 검사 포함 에피소드 실행 중입니다. 정지한 뒤 조작하세요.')
        if action not in ('hold','release'):require_calibrated(self.profile)
        if not isinstance(self.session,MotionSession):raise ValueError('팔로워를 먼저 연결하세요.')
        if action in ('hold','release'):
            self.stop_leader_assist()
            # A visual/vision cleanup failure must never intercept the stop.
            try:return self.session.request(action,targets)
            finally:
                try:
                    if self.inspection_run:self.inspection_run.cancel()
                except Exception as exc:self.notice('검사 정리 실패: '+str(exc),True)
                try:self.live_adjust.stop(halt=False);self.stop_preview(quiet=True)
                except Exception as exc:self.notice('정지 후 화면 정리 실패: '+str(exc),True)
        if action in ('arm','follow','play'):self.live_adjust.stop(halt=False)
        if action=='follow':
            if not self.session.running or self.session.state!='HOLD':raise ValueError('현재 자세 유지 상태에서 리더 따라가기를 시작하세요.')
            return self.request_follow_jig_read()
        was_frozen=self.detector.frozen
        if action in ('move','play'):self.set_pose_frozen(True)
        try:request_id=self.session.request(action,targets)
        except Exception:
            if not was_frozen and action in ('move','play'):
                try:self.set_pose_frozen(False)
                except Exception as exc:self.notice('지그 고정 해제 확인 실패: '+str(exc),True)
            raise
        bridge=getattr(self.workspace_manager,'integration',None)
        if bridge and action in ('move','play'):
            try:bridge.coordinator.submitted(arm_id(self.profile),action,request_id,targets)
            except Exception:
                self.session.request('hold')
                raise RuntimeError('통합 실행 기록 저장 실패 · 이동 정지 요청')
        if action in ('arm','move','play','follow'):
            self.taught_jig_display=None;self.measured_jig_display=None;self.measured_target_active=False;self.preview_jig_references=None
        if action=='follow':
            self.measured_jig_display=self.held_teaching_scene_references();self.measured_target_active=True
        if action in ('arm','move','play'):
            try:self.stop_preview(quiet=True);self.set_mode('live')
            except Exception as exc:self.notice('실물 요청 전송 완료 · 화면 갱신 실패: '+str(exc),True)
        return request_id
    def execute_target(self):
        ticks=self.calibration.ticks({n:int(v.get()) for n,v in self.tick_vars.items()})
        original=next((s for s in self.episode['steps'] if s['id']==self.selected),None)
        self.prepare_execution('move',[self.with_jig(self.store.step(ticks,self.step_name.get()),original=original)])
    def require_inspection_start(self):
        if self.inspection_run and self.inspection_run.busy:raise ValueError('안착 검사 포함 에피소드가 실행 중입니다.')
        from .episode_inspection import validate_inspection,has_inspections
        validate_inspection(self.episode)
        from .episode_inspection import inspection_schedule
        inspection_schedule(self.episode['steps'])
        if has_inspections(self.episode):
            from .episode_inspection_runtime import require_resources
            require_resources()
        self.inspection_run=None
    def execute_episode(self):
        if self.remote_mode and self.profile.get('mode')!='demo':
            if self.camera_only:raise ValueError('카메라 전용에서는 에피소드를 실행할 수 없습니다.')
            from .pi_execution import PiExecution
            PiExecution(self);return
        self.require_inspection_start()
        self.live_adjust.stop()
        steps=deepcopy(self.episode['steps']);self.store.validate(self.episode)
        from .episode_inspection import has_inspections
        if has_inspections(self.episode):
            self.prepare_execution('startup',steps);return
        self.continue_episode(steps,apply_jig=True)
    def startup_inspection(self,steps,*,apply_jig,current=None):
        if not self.episode.get('startup_inspections'):return False
        if self.camera_only:raise ValueError('카메라 전용에서는 에피소드를 실행할 수 없습니다.')
        from .episode_execution import StartupInspectionRun
        self.inspection_run=StartupInspectionRun(self,lambda:self.continue_episode(steps,apply_jig=apply_jig,current=current),fixed_jigs=current);self.inspection_run.start();return True
    def continue_episode(self,steps,*,apply_jig,current=None):
        if not apply_jig:
            if current is not None:self.begin_execution('play',steps,current,apply_jig=False)
            else:self.prepare_execution('play',steps,apply_jig=False)
            return
        if steps and steps[0].get('safe_boundary')=='start':
            request_id=self.motion_request('move',[steps[0]['ticks']]);self.safe_entry={'steps':steps[1:],'page':self.page,'request_id':request_id,'current':deepcopy(current)};return
        if current is not None:self.begin_execution('play',steps,current);return
        self.prepare_execution('play',steps)
    def execute_taught_episode(self):
        if self.remote_mode and self.profile.get('mode')!='demo':
            if self.camera_only:raise ValueError('카메라 전용에서는 에피소드를 실행할 수 없습니다.')
            from .pi_execution import PiExecution
            PiExecution(self,apply_jig=False);return
        self.require_inspection_start()
        self.live_adjust.stop()
        # Keep references in the episode and audit record; bypass only this run's IK.
        if self.safe_entry:raise ValueError('안전 자세로 이동 중입니다. 현재 실행이 끝난 후 다시 실행하세요.')
        steps=deepcopy(self.episode['steps']);self.store.validate(self.episode)
        from .episode_inspection import has_inspections
        if has_inspections(self.episode):self.prepare_execution('startup',steps,apply_jig=False);return
        self.continue_episode(steps,apply_jig=False)
    def stop_all(self):
        if getattr(getattr(self.workspace_manager,'safe_shutdown',None),'busy',False):
            self.guard(lambda:self.motion_request('hold'),during_shutdown=True);return
        self.stop_leader_assist()
        if isinstance(self.session,MotionSession) and self.session.running:self.motion_request('release')
        else:self.live_adjust.stop(halt=False);self.stop_preview(quiet=True)
        self.notice('토크 해제 요청 · 모터의 OFF 확인 결과를 확인하세요.')
    def stop_preview(self,quiet=False):
        if not quiet:self.live_adjust.stop()
        if self.remote_plan_job:self.remote_plan_job[0].cancel();self.remote_plan_job=None
        camera_return=(self.pending_execution or self.camera_task or {}).get('page')
        self.camera_task=None;self.playing=False;self.pending_execution=None;self.safe_entry=None
        if self.live_adjust.owner is None and not (isinstance(self.session,MotionSession) and self.session.program_active.is_set()):
            self.set_pose_frozen(False);self.taught_jig_display=None
        if camera_return is not None:self.show_page(camera_return,internal=True)
        if camera_return is not None and self.page!='camera':self.suspend_camera()
        if not quiet:self.notice('3D 미리보기 정지 · 실물 정지는 별도 버튼 또는 Esc를 사용하세요.')
    def reset_view(self):self.view=overhead_view(self.profile);self.last_render=None
    def workcell_view(self):
        from .workcell_preview import WORKCELL_VIEW
        self.view=WORKCELL_VIEW;self.last_render=None
    def bind_pan(self,canvas):
        for button in (2,3):
            canvas.bind(f'<ButtonPress-{button}>',lambda e:setattr(self,'pan_start',(e.x,e.y,self.view)))
            canvas.bind(f'<B{button}-Motion>',self.pan_view)
        canvas.bind('<Shift-ButtonPress-1>',lambda e:setattr(self,'pan_start',(e.x,e.y,self.view)))
        canvas.bind('<Shift-B1-Motion>',self.pan_view)
    def pan_view(self,event):
        if getattr(self,'pan_start',None):
            x,y,view=self.pan_start
            height=min(event.widget.winfo_height(),event.widget.winfo_width()*PREVIEW_SIZE[1]/PREVIEW_SIZE[0])
            self.view=pan_view(view,event.x-x,event.y-y,height);self.last_render=None
        return 'break'
    def rotate_view(self,event):
        if self.drag_start:
            x,y,view=self.drag_start;self.view=(view[0]-(event.x-x)*.5,max(-80,min(20,view[1]-(event.y-y)*.3)),*view[2:]);self.last_render=None
    def zoom(self,factor):self.view=(*self.view[:2],max(.35,min(2.,self.view[2]*factor)),*self.view[3:]);self.last_render=None
    def start_camera(self):
        from .pi_execution import active_execution
        job=active_execution(self)
        if job:
            if job.thread is None or self is not job.app:return
            if self.camera and self.camera.running:return
            from .remote_client import RemoteCameraSession
            self.camera_manual_off=False;self.camera_sleeping=False;self.camera_close_requested=False
            self.camera=RemoteCameraSession(self.remote,observer=True);self.camera.start();return
        self.camera_manual_off=False
        if self.page=='camera':self.camera_view_requested=True
        if self.workspace_manager and not self.workspace_manager.claim_camera(self):
            self.camera_waiting_for_arm=True;self.camera_restart_pending=True;return
        if self.camera_waiting_for_arm:
            now=time.monotonic()
            for task in (self.camera_task,self.pending_execution):
                if task:task['started']=now;task['budget_started']=now
            self.camera_waiting_for_arm=False
        self.camera_manual_off=False
        if self.page=='camera':self.camera_view_requested=True
        if self.camera and self.camera.running:
            if self.camera_close_requested:self.camera_restart_pending=True
            self.update_camera_mode()
            return
        self.camera_manual_off=False;self.camera_sleeping=False;self.camera_close_requested=False
        from .vision import detect
        if self.remote_mode:
            from .remote_client import RemoteCameraSession
            link=self.require_remote();link.sync(self);self.camera=RemoteCameraSession(link)
        else:self.camera=CameraSession(**self.profile['camera'],processor=self.detector.process)
        self.update_camera_mode();self.camera.start();self.last_camera_at=0
    def fit_image(self,canvas,rgb,*,resample=Image.Resampling.LANCZOS):
        image=Image.fromarray(rgb) if not isinstance(rgb,Image.Image) else rgb
        w,h=max(1,canvas.winfo_width()),max(1,canvas.winfo_height());scale=min(w/image.width,h/image.height)
        size=(max(1,round(image.width*scale)),max(1,round(image.height*scale)));x=(w-size[0])//2;y=(h-size[1])//2
        photo=ImageTk.PhotoImage(image.resize(size,resample),master=canvas)
        existing=canvas.find_withtag('image')
        if existing:
            canvas.itemconfigure(existing[0],image=photo);canvas.coords(existing[0],x,y)
        else:canvas.create_image(x,y,image=photo,anchor='nw',tags='image')
        canvas.tag_lower('image');canvas.photo=photo
        return x,y,*size
    def image_point(self,x,y):
        if self.camera_rect is None:return None
        left,top,w,h=self.camera_rect
        return (max(0.,min(1.,(x-left)/w)),max(0.,min(1.,(y-top)/h)))
    def refresh_jig_reference(self):
        since=time.monotonic()
        if not self.detector.clear():raise ValueError('실행 중에는 지그 위치를 고정합니다.')
        self.measurement_valid_after=since
    def roi_edit_allowed(self):
        if self.workspace_manager:
            try:self.workspace_manager.require_shared_jig_idle(self)
            except ValueError as exc:self.notice(str(exc));return False
        if self.camera_all.get():self.notice('범위를 바꿀 지그를 목록에서 선택하세요.');return False
        if self.pose_latch.frozen or self.jig_measurement_in_use():
            self.notice('측정·실행 중에는 감지 범위를 유지합니다.');return False
        return True
    def change_roi_mode(self):
        self.cancel_roi_points();self.preferences['roi_draw_mode']=self.roi_mode.get();atomic_json(self.data_dir/'preferences.json',self.preferences);self.update_camera_profile_text()
    def roi_canvas_point(self,e):
        # Clamping lets a stroke begin or end beyond the visible image edge.
        return self.image_point(e.x,e.y)
    def roi_press(self,e):
        if not self.roi_edit_allowed():return
        point=self.roi_canvas_point(e)
        if point is None:return
        self.roi_start=point;self.roi_points=[point];self.roi_hover=None
        self.roi_progress.set('놓으면 저장 · 우클릭 취소');self.draw_roi_draft()
    def roi_move(self,e):
        if self.roi_start is None:return
        if not self.roi_edit_allowed():self.cancel_roi_points();return
        point=self.roi_canvas_point(e)
        if point is None:return
        if self.roi_mode.get()=='사각형':
            a=self.roi_start;x0,y0=min(a[0],point[0]),min(a[1],point[1]);x1,y1=max(a[0],point[0]),max(a[1],point[1])
            self.roi_points=[[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
        else:
            last=self.roi_points[-1]
            if abs(point[0]-last[0])+abs(point[1]-last[1])>.001:self.roi_points.append(point)
            if len(self.roi_points)>2048:self.roi_points=self.roi_points[::2]+[self.roi_points[-1]]
        self.draw_roi_draft()
    def roi_release(self,e):
        if self.roi_start is None:return
        if not self.roi_edit_allowed():self.cancel_roi_points();return
        start=self.roi_start;self.roi_move(e);point=self.roi_canvas_point(e);points=list(self.roi_points)
        self.cancel_roi_points()
        try:
            if self.roi_mode.get()=='사각형':
                region=[min(start[0],point[0]),min(start[1],point[1]),max(start[0],point[0]),max(start[1],point[1])]
                if region[2]-region[0]<.01 or region[3]-region[1]<.01:raise ValueError('마우스를 끌어 사각형 범위를 지정하세요.')
            else:
                from .roi_geometry import freehand_roi
                obs=self.camera_view_observation();height,width=obs[0].shape[:2] if obs else (720,1280)
                region=freehand_roi(points,width,height)
        except ValueError as exc:self.notice(str(exc),True);return
        self.guard(lambda:self.apply_camera_roi(region))
    def cancel_roi_points(self):
        self.roi_points=[];self.roi_hover=None;self.roi_start=None
        if hasattr(self,'roi_progress'):self.roi_progress.set('지그 선택 후 범위 편집' if self.camera_all.get() else '드래그 후 놓으면 저장')
        self.camera_canvas.delete('roi-draft')
    def clear_camera_roi(self):
        if not self.roi_edit_allowed():return
        self.apply_camera_roi(None)
    def apply_camera_roi(self,region):
        from .roi_geometry import roi_vertices
        roi_vertices(region)
        self.roi=region;self.cancel_roi_points();self.save_preferences()
        # RemoteDetector.clear synchronizes the newly persisted region.
        if not self.detector.clear(self.active_jig):raise ValueError('측정·실행 중에는 감지 범위를 유지합니다.')
        self.draw_roi();self.notice('감지 범위 저장 · 최신 검출을 확인하세요.' if region else '전체 영상에서 현재 지그를 감지합니다.')
    def draw_roi_draft(self):
        c=self.camera_canvas;c.delete('roi-draft')
        if not self.camera_rect or not self.roi_points:return
        left,top,w,h=self.camera_rect;points=[(left+x*w,top+y*h) for x,y in self.roi_points]
        if len(points)>1:
            c.create_line(*[v for point in points for v in point],fill='#E1A13B',width=3,tags='roi-draft')
            if len(points)>2:c.create_line(*points[-1],*points[0],fill='#E1A13B',width=2,dash=(5,3),tags='roi-draft')
        x,y=points[0];c.create_oval(x-4,y-4,x+4,y+4,fill='#E1A13B',outline='white',tags='roi-draft')
    def draw_roi(self):
        c=self.camera_canvas;c.delete('roi')
        snapshot=getattr(self,'camera_display_snapshot',None)
        if snapshot is not None and snapshot.get('jig_id')!=self.camera_view_key():
            self.camera_display_snapshot=None;c.delete('image');return
        if snapshot is not None:
            import cv2
            from .vision import annotate_roi
            snapshot['roi']=None if self.camera_all.get() else deepcopy(self.roi)
            snapshot['image']=annotate_roi(snapshot['base_image'],snapshot['roi'])
            self.camera_rect=self.fit_image(c,cv2.cvtColor(snapshot['image'],cv2.COLOR_BGR2RGB),resample=Image.Resampling.BILINEAR)
        self.draw_roi_draft()
    def save_preferences(self):
        if self.episode.get('steps'):self.preferences.setdefault('last_episode_by_arm',{})[arm_id(self.profile)]=self.episode['id']
        changed=self.catalog.items[self.active_jig].get('roi')!=self.roi
        self.catalog.items[self.active_jig]['roi']=self.roi;atomic_json(self.catalog.path,self.catalog.items);self.catalog.revision+=1
        self.preferences['camera_roi']=self.roi;self.preferences['camera_source']=self.profile['camera']['source'];atomic_json(self.data_dir/'preferences.json',self.preferences)
        if changed and self.workspace_manager:self.workspace_manager.sync_catalog(self)
    def save_camera(self):
        obs=self.camera_view_observation()
        if not obs or time.monotonic()-obs[2]>1:raise ValueError('최신 카메라 영상이 없습니다.')
        snapshot=getattr(self,'camera_display_snapshot',None)
        if snapshot is None or snapshot['jig_id']!=self.camera_view_key() or time.monotonic()-snapshot['at']>1:
            self.draw_camera_frame(time.monotonic(),for_capture=True);snapshot=self.camera_display_snapshot
        import cv2
        from datetime import datetime
        folder=self.data_dir/'captures';name=f"{datetime.now():%Y-%m-%d_%H-%M-%S_%f}"
        p=folder/'화면 표시본'/(name+'.png');raw=folder/'원본'/(name+'.png');meta=folder/'판정 기록'/(name+'.json')
        p.parent.mkdir(parents=True,exist_ok=True);raw.parent.mkdir(parents=True,exist_ok=True)
        try:
            if not cv2.imwrite(str(p),snapshot['image']):raise OSError('영상 저장 실패')
            if not cv2.imwrite(str(raw),snapshot['raw_image']):raise OSError('원본 영상 저장 실패')
            atomic_json(meta,{'camera':self.profile['camera'],'frame_monotonic':snapshot['at'],'detection_monotonic':snapshot.get('detection_at'),'jig_id':snapshot['jig_id'],'roi':snapshot['roi'],'detection':snapshot['result'],'calibration_sha256':self.calibration.sha256,'raw_image':str(raw.relative_to(folder)),'annotated_image':str(p.relative_to(folder))})
        except Exception:
            for file in (p,raw,meta):file.unlink(missing_ok=True)
            raise
        self.camera_save_path.set(str(p.resolve()))
        self.notice('원본·화면 표시본 저장됨 · '+str(p.resolve()))
        return p
    def open_capture_folder(self):
        import subprocess
        folder=(self.data_dir/'captures').resolve();folder.mkdir(parents=True,exist_ok=True)
        subprocess.Popen(['xdg-open',str(folder)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def live_preview_sample(self):
        # Rendering consumes the latest received sample; monitor widgets refresh separately.
        session=self.session
        sample=getattr(session,'latest',None) or self.latest
        if (session and session.running and not getattr(session,'error',None)
                and not getattr(self.remote,'error',None) and sample and sample.fresh()
                and sample.calibration_matches and sample.calibration_sha256==self.calibration.sha256):return sample
        return None
    def live_unavailable_reason(self):
        if self.session and self.session.error:
            return '실물 연결 오류\n'+self.session.error+'\n장비 모니터링에서 상태를 확인하세요.'
        if not self.session or not self.session.running:
            return '팔로워 연결이 필요합니다.\n연결 후 현재 모터값으로 3D 자세를 표시합니다.\n연결만으로 토크가 켜지지는 않습니다.'
        if self.latest and not self.latest.calibration_matches:
            return '모터 영점과 선택한 JSON이 다릅니다.\n설정 · 안내에서 기준 파일을 확인하세요.'
        return '현재 모터값 수신 대기\n현재 위치를 확인할 수 없습니다.'
    def render_tick(self):
        if self.closed:return
        if not self.workspace_visible:
            self.render_job=self.root.after(200,self.render_tick);return
        started=time.monotonic();canvas=self.model_canvas if self.page=='model' else self.preview_canvas
        try:
            if self.page=='camera':self.draw_camera_frame(started,scheduled=True)
            sample=self.live_preview_sample();fresh=sample is not None
            placement=self.workspace_manager.live_workcell(self) if self.workspace_manager else deepcopy(self.workcell_preview)
            if self.mode!='live':
                from .workcell_preview import teaching_placement
                placement=teaching_placement(placement,self.profile)
            if placement and self.workspace_manager and self.mode=='live':placement['active_arm_visible']=fresh
            visible=(fresh,(placement or {}).get('other_arm_visible',True))
            if self.mode=='live' and visible!=getattr(self,'live_visibility',None):
                self.render_context+=1;self.last_render=None
                self.pending_render_context=self.render_context if self.last_rgb is not None else None
            self.live_was_fresh=fresh;self.live_visibility=visible
            if self.playing:
                ticks,done=self.transport.sample();self.apply_target(ticks,preview=True)
                self.transport_label.set(f'{self.transport.elapsed:.1f} / {self.transport.duration:.1f}초');self.transport_sync=True;self.timeline.set(self.transport.elapsed);self.transport_sync=False
                if done:self.stop_preview(quiet=True);self.notice('경로 미리보기 완료')
            if self.mode=='live':
                other_live=bool(placement and placement.get('other_arm_dynamic') and placement.get('other_arm_visible'))
                ticks=sample.ticks if fresh else self.reference.middle if other_live else None
                self.preview_caption.set('실물값 보기 · 최신 틱' if fresh else '실물값 보기 · 선택한 팔 현재값 없음')
            else:
                right=self.second_editor if self.preview_editor==2 and self.second_editor and not self.playing and self.page=='teach' else None
                ticks=right.target if right else self.target
                self.preview_caption.set('경로 미리보기 · 실물 구동 없음' if self.playing else ('스텝 자세 편집 2' if right else '스텝 자세')+' · 실물 구동 없음')
            if ticks is not None:
                mapping=self.calibration.angle_mapping
                if mapping and any(mapping.extrapolated(n,ticks[n]) for n in JOINTS):self.preview_caption.set(self.preview_caption.get()+' · 기준각 밖 추정')
                angles=self.reference.angles(ticks);jig={'jigs':[],'tcp':self.profile.get('tcp'),'camera_fovy':overhead_fovy(self.profile)};results={}
                saved_references=self.saved_scene_references();measured_references=self.measured_scene_references()
                if saved_references is not None:self.preview_caption.set(self.preview_caption.get()+(' · 지그: 티칭 저장 기준' if saved_references else ' · 지그: 저장 기준 없음'))
                elif measured_references is not None:self.preview_caption.set(self.preview_caption.get()+' · 지그: '+('경로 계산 기준' if self.mode=='target' and self.preview_jig_references is not None else '실행 측정 기준'))
                results=self.camera_adoption_results(started)
                if saved_references is None and measured_references is None:self.preview_caption.set(self.preview_caption.get()+' · 지그: 현재 채택 기준')
                for key,config in self.catalog.items.items():
                    mesh=self.catalog.mesh(key);raw=results.get(key,{});reference_used=None;mesh_yaw_offset=0.
                    if saved_references is not None:
                        saved=saved_references.get(key);reference_used=saved;pose=deepcopy(saved['pose']) if saved else None
                    elif measured_references is not None:
                        reference=measured_references.get(key);reference_used=reference;pose=deepcopy(reference['pose']) if reference else None
                    else:
                        chosen=raw.get('selected')  # The simulation shows the exact accepted pose.
                        metric=chosen.get('metric') if chosen else None
                        pose=[*metric['center_xy_mm'],metric['yaw_deg']] if metric else None
                        mesh_yaw_offset=metric.get('mesh_yaw_offset_deg',0.) if metric else 0.
                    if pose and reference_used:
                        mesh_yaw_offset=reference_used.get('mesh_yaw_offset_deg',0.)
                        if mesh.get('shape')=='rectangle' and mesh.get('assembly',{}).get('orientation_hole'):
                            # Historical carrier teaching keeps its semantic pose,
                            # while the visual uses the currently registered assembly.
                            if reference_used.get('symmetry_deg')==180:mesh_yaw_offset=180.
                            if reference_used.get('stl_sha256')!=mesh['sha256'] or reference_used.get('symmetry_deg')==180:
                                self.preview_caption.set(self.preview_caption.get()+' · 운반용 지그: 현재 모델')
                        else:
                            saved_mesh=self.catalog.mesh_for_reference(key,reference_used)
                            if saved_mesh is None:pose=None
                            else:mesh=saved_mesh
                    jig['jigs'].append({'id':key,'stl':mesh['stl_path'],'unit':mesh['unit'],'low_mm':mesh['low_mm'],'size_mm':mesh['size_mm'],
                        'bottom_z_mm':support_bottom_z(config,self.profile),'mesh_yaw_offset_deg':mesh_yaw_offset,
                        'pose':pose,'platform':config.get('physical_installation',{}).get('platform'),'platform_height_mm':config.get('physical_installation',{}).get('carrier_underside_height_mm'),'platform_yaw_deg':config.get('physical_installation',{}).get('platform_yaw_deg',-90.)})
                if placement:
                    jig['workcell']=placement
                    placement_label='배치 확정' if placement.get('confirmed_for_preview') else '배치 추정'
                    other='2' if arm_id(self.profile)=='arm3' else '3'
                    pose_label={'live':'최신 실물값','unavailable':'현재값 없음 · 숨김','safe':'시작 안전 자세 (표시 전용)','reference':'기준 자세 · 안전 자세 미설정'}.get(placement.get('other_arm_pose_kind'),'자세 예시 (표시 전용)')
                    self.preview_caption.set(self.preview_caption.get()+f' · 로봇팔{other}: '+placement_label+' · '+pose_label)
                    if calibration_pending(self.profile):self.preview_caption.set(self.preview_caption.get()+' · 선택 팔 보정 필요')
                    if self.workcell_preview.get('linear_stage',{}).get('state_source'):
                        from .linear_state import state_label
                        self.preview_caption.set(self.preview_caption.get()+' · '+('미리보기 · ' if self.mode!='live' else '')+state_label(placement))
                request=(angles,self.view,jig,self.profile.get('table_z_mm',-7.4))
                fk_key=(angles,tuple((placement or {}).get('other_arm_joint_angles_rad',())),visible,(placement or {}).get('other_arm_pose_kind'))
                if fk_key!=self.last_fk_angles:
                    xyz=display_position(self.kin.fk(ticks)[:3,3])
                    label=f'CAD 집게 끝   X→ {xyz[0]:.1f}   Y↑ {xyz[1]:.1f}   바닥↑Z {height_from_base_z(xyz[2],self.profile):.1f} mm'
                    if self.workcell_tcp is not None:
                        from .arm_workspace import tcp_positions,preview_profile
                        points=tcp_positions(self.kin.fk(ticks),self.profile,placement);floor_profile=preview_profile(self.profile);labels=[]
                        for key in ('arm2','arm3'):
                            active=key==arm_id(self.profile)
                            available=(fresh if active else (placement or {}).get('other_arm_visible',True)) if self.mode=='live' else True
                            if not available:labels.append(f'팔{key[-1]} TCP — 현재값 없음');continue
                            point=display_position(points[key]);kind=(placement or {}).get('other_arm_pose_kind')
                            example='' if active or kind=='live' else '(안전)' if kind=='safe' else '(기준)' if kind=='reference' else '(예시)'
                            labels.append(f'팔{key[-1]} TCP{example}  X {point[0]:.1f}  Y {point[1]:.1f}  Z↑ {height_from_base_z(point[2],floor_profile):.1f} mm')
                        label='\n'.join(labels)
                    self.tcp_label.set(label);self.last_fk_angles=fk_key
                if self.render_enabled and self.page in ('teach','model') and request!=self.last_render:
                    if self.renderer is None:self.renderer=Renderer()
                    if self.renderer.submit(*request,context=self.render_context):self.last_render=request
            elif self.mode=='live':
                reason=self.live_unavailable_reason();self.preview_caption.set('실물값 보기 · '+reason.splitlines()[0])
                if self.last_rgb is not None:reason+='\n마지막 수신 화면 · 현재 위치 아님'
                if canvas.find_withtag('stale'):canvas.itemconfigure('stale',text=reason,width=max(100,canvas.winfo_width()-40))
                else:canvas.create_text(20,30,text=reason,anchor='nw',fill=COLORS['ink'],width=max(100,canvas.winfo_width()-40),tags='stale')
                box=canvas.bbox('stale')
                if box:
                    x0,y0,x1,y1=box;bounds=(x0-8,y0-6,x1+8,y1+6)
                    if canvas.find_withtag('stale_bg'):canvas.coords('stale_bg',*bounds)
                    else:canvas.create_rectangle(*bounds,fill='white',outline=COLORS['muted'],tags='stale_bg')
                    canvas.tag_raise('stale_bg');canvas.tag_raise('stale')
                self.tcp_label.set('CAD 집게 기준점 —');self.last_fk_angles=None
            if ticks is not None and self.pending_render_context==self.render_context:
                self.preview_caption.set('3D 자세 갱신 중 · 이전 화면 표시')
            if self.renderer:
                item=self.renderer.poll()
                if item:
                    if item[0]=='error':self.notice('3D 표시 실패: '+item[1],True)
                    elif ticks is not None and item[1]>self.displayed_token and item[3]==self.render_context and (self.mode!='live' or time.monotonic()-item[4]<=.5):
                        self.displayed_token=item[1];self.pending_render_context=None
                        self.last_rgb=Image.open(io.BytesIO(item[2])).copy();self.fit_image(canvas,self.last_rgb);canvas.delete('stale');canvas.delete('stale_bg')
                    elif item[0]=='frame' and ticks is not None and item[3]==self.render_context and self.mode=='live':
                        # GL startup can take longer than the live-pose age limit. Retry even if ticks stay still.
                        self.last_render=None
        except Exception as exc:self.notice('3D 갱신 오류: '+str(exc),True)
        self.render_job=self.root.after(frame_delay_ms(started,time.monotonic()),self.render_tick)

    def held_teaching_scene_references(self):
        references={key:self.current_jig_reference(key) for key in self.teaching_jig_results}
        return deepcopy({key:value for key,value in references.items() if value is not None})

    def measured_scene_references(self):
        if self.mode=='target' and self.preview_jig_references is not None:
            return deepcopy(self.preview_jig_references)
        if getattr(self.session,'state',None)=='FOLLOW' and self.jig_updates_paused:
            return self.held_teaching_scene_references()
        if self.mode=='live' or self.measured_target_active:
            return deepcopy(self.measured_jig_display)
        return None

    def saved_scene_references(self):
        state=self.taught_jig_display
        if state is not None and (self.mode!='target' or state.get('target_active',True)):
            from .jig_comparison import teaching_scene_references
            matched=state['request_id'] is not None and getattr(self.session,'active_request_id',None)==state['request_id']
            index=getattr(self.session,'index',0) if matched else 0
            return teaching_scene_references(state['steps'],index)
        if self.mode!='target' or self.page!='teach' or self.measured_scene_references() is not None:return None
        # Calculated paths and live adjustment use the measured scene. Ordinary
        # draft editing must remain anchored to the corresponding teaching pose.
        if (self.playing or self.preview_jig_references is not None or self.pending_execution or self.remote_plan_job or self.safe_entry
                or self.live_adjust.owner is not None):return None
        editor=self.second_editor if self.preview_editor==2 and self.second_editor else self
        keys={step['jig_id'] for step in self.episode['steps'] if step.get('jig_id')}
        references={key:deepcopy(self.ensure_teaching_reference(key)) for key in keys}
        references={key:value for key,value in references.items() if value is not None}
        if not editor.follow_jig.get():return references or None
        key=editor.selected_jig_id()
        selected=(getattr(editor,'original',{}) if editor is not self else
                  next((s for s in self.episode['steps'] if s['id']==editor.selected),{}))
        reference=selected.get('jig_reference') if selected.get('jig_id')==key else None
        reference=reference or self.ensure_teaching_reference(key)
        if reference:references[key]=deepcopy(reference)
        return references

    def camera_view_key(self):return None if self.camera_all.get() else self.active_jig
    def update_camera_profile_text(self):
        if self.camera_all.get():
            self.camera_profile_text.set('등록한 모든 지그의 최신 검출 결과를 함께 표시합니다.\n범위를 바꾸려면 지그를 선택하거나 오른쪽 목록을 더블클릭하세요.');return
        m=self.mesh_profile;sx,sy=m['size_mm'][:2]
        instruction='드래그해서 사각형을 지정하세요.' if self.roi_mode.get()=='사각형' else '지그 주위를 여유 있게 둘러 그리고 마우스를 놓으세요.'
        self.camera_profile_text.set(f"{instruction} 영상 밖으로 끌면 가장자리에 맞춥니다. · 우클릭 취소\n{self.catalog.items[self.active_jig]['name']} · STL {sx:g}×{sy:g} mm · 받침 높이 {support_height(self.catalog.items[self.active_jig],self.profile):g} mm (바닥=0)")
    def change_camera_view(self,persist=True):
        self.cancel_roi_points();all_jigs=self.camera_all.get()
        self.camera_jig_choice.current(0 if all_jigs else list(self.catalog.items).index(self.active_jig)+1)
        self.roi_mode_choice.configure(state='disabled' if all_jigs else 'readonly')
        for button in (self.roi_cancel_button,self.roi_clear_button):button.state(['disabled'] if all_jigs else ['!disabled'])
        if all_jigs:self.camera_overview.grid();self.update_camera_overview({})
        else:self.camera_overview.grid_remove()
        self.refresh_adoption_display();self.update_camera_profile_text();self.camera_display_snapshot=None;self.camera_canvas.delete('image')
        if persist:self.preferences['camera_all_jigs']=all_jigs;atomic_json(self.data_dir/'preferences.json',self.preferences)
        self.draw_camera_frame(time.monotonic())
    def open_camera_overview_jig(self,event):
        key=self.camera_jig_status.identify_row(event.y)
        if key not in self.catalog.items:return
        def open_view():
            self.select_camera_jig(key);self.camera_all.set(False);self.change_camera_view()
        self.guard(open_view)
    def camera_view_results(self,data):
        if not data:return {}
        if data.get('status')=='paused':return {key:{'selected':None,'status':'paused','live_view':True} for key in self.catalog.items}
        source=data.get('live_by_jig',data.get('by_jig',{self.active_jig:data}))
        return {key:{**value,'live_view':True} for key,value in source.items() if key in self.catalog.items}
    def adoption_labels(self):
        from .adoption_display import adoption_text
        now=time.monotonic();accepted=self.camera_adoption_results(now)
        return {key:adoption_text(accepted.get(key),now,self.pose_latch.seconds) for key in self.catalog.items}
    def refresh_adoption_display(self):
        enabled=self.show_adoption.get()
        self.camera_jig_status.configure(displaycolumns=('name','status','adoption') if enabled else ('name','status'))
        if enabled:self.camera_adoption_label.grid()
        else:self.camera_adoption_label.grid_remove()
        if enabled:self.update_adoption_caption()
    def update_adoption_caption(self):
        legend='채택 위치는 실선 · 최신 검출 외곽은 점선'
        self.camera_adoption_text.set(legend if self.camera_all.get() else '채택 상태 · '+self.adoption_labels().get(self.active_jig,'미채택')+'  |  '+legend)
    def camera_adopted_positions(self,now):
        from .adoption_display import drawable_adoptions
        if not self.show_adoption.get():return {}
        accepted=drawable_adoptions(self.camera_adoption_results(now),now,self.pose_latch.seconds)
        return accepted if self.camera_all.get() else {key:value for key,value in accepted.items() if key==self.active_jig}
    def toggle_adoption_display(self):
        self.preferences['camera_show_adoption']=self.show_adoption.get();atomic_json(self.data_dir/'preferences.json',self.preferences)
        self.refresh_adoption_display()
        observation=self.camera_view_observation()
        results=self.camera_view_results(observation[1]) if observation and 0<=time.monotonic()-observation[2]<1 else {}
        self.update_camera_overview(results);self.draw_camera_frame(time.monotonic())
    def update_camera_overview(self,results):
        now=time.monotonic()
        context=(self.catalog.revision,self.camera_all.get(),self.show_adoption.get())
        urgent=not results or any(v.get('status') in ('error','calibration_mismatch','paused') for v in results.values())
        if not urgent and context==getattr(self,'camera_overview_context',None) and now-getattr(self,'camera_overview_updated_at',float('-inf'))<1:return
        self.camera_overview_context=context
        self.camera_overview_updated_at=now if results else float('-inf')
        from .adoption_display import adoption_summary
        table=self.camera_jig_status;known=set(table.get_children());detected=0;adoption=self.adoption_labels() if self.show_adoption.get() else {}
        for index,(key,config) in enumerate(self.catalog.items.items(),1):
            result=results.get(key);status='영상 대기';tag='waiting'
            if result:
                if result.get('selected'):
                    status='유지값' if result.get('pose_held') else '부분 감지' if result['selected'].get('partial_visible') else '감지됨';tag='waiting' if result.get('pose_held') else 'detected';detected+=not result.get('pose_held',False)
                    if not result.get('pose_held'):
                        if result['selected'].get('raised_features_verified'):status='돌출부 확인'
                        if result['selected'].get('grid_verified'):status='교차점 확인'
                        if result['selected'].get('orientation_verified'):status='방향 확인'
                else:
                    status={'ambiguous':'후보 여러 개','orientation_unconfirmed':'방향 확인 중','processing':'갱신 중','paused':'검출 정지','calibration_mismatch':'보정 확인','error':'검출 오류','settings_changed':'설정 적용 중'}.get(result.get('status'),'미검출');tag='waiting' if result.get('status') in ('processing','settings_changed','paused') else 'missing'
                if result.get('display_pending'):status='갱신 중';tag='waiting'
            full=adoption.get(key,'')
            self.camera_overview_details[key]=config['name']+'\n현재 상태: '+status+('\n채택 상태: '+full if full else '')
            values=(f"{index} · {config['name']}",status,adoption_summary(full))
            if key not in known:table.insert('','end',iid=key,values=values,tags=(tag,))
            elif tuple(table.item(key,'values'))!=values:table.item(key,values=values,tags=(tag,))
            if key in known:known.remove(key)
        for key in known:table.delete(key)
        self.camera_overview_title.set(f'전체 지그 · 감지 {detected}/{len(self.catalog.items)}')
        self.show_camera_overview_detail()
    def show_camera_overview_detail(self,event=None):
        selected=self.camera_jig_status.selection()
        self.camera_overview_detail.set(self.camera_overview_details.get(selected[0],'영상 대기') if selected else '행 선택: 상세 상태 · 더블클릭: 범위 편집')
    def camera_view_observation(self):
        if not self.camera:return None
        return getattr(self.camera,'preview_observation',None) or self.camera.observation
    def camera_view_result(self,data):
        if not data:return data
        if data.get('status')=='paused':return {**data,'live_view':True,'error':'검출 정지 · 고정 위치 유지'}
        if 'live_by_jig' in data:
            result=data['live_by_jig'].get(self.active_jig,{'selected':None,'candidates':[],'status':'processing','error':'검출 갱신 중'})
            return {**result,'live_view':True}
        if data.get('_preview'):return {**data,'live_view':True}
        return data.get('by_jig',{}).get(self.active_jig,{'selected':None,'candidates':[]}) if 'by_jig' in data else data
    def camera_display_results(self,results,now,*,raw=False):
        """Interpolate outlines only; measurements, acceptance and captures stay raw."""
        from .camera_display import display_results
        return display_results(self.camera_display_poses,results,now,raw=raw)
    def draw_camera_frame(self,now,*,scheduled=False,for_capture=False):
        inspection=self.camera_tabs.select()==str(self.camera_inspection) and not for_capture
        # CAD is a static reference. Do not resize live images behind that tab.
        if inspection and self.camera_inspection.mode.get()=='CAD 설명':return
        obs=self.camera_view_observation()
        if not obs or not 0<=now-obs[2]<1:
            self.camera_display_poses.clear()
            self.camera_paint_key=None
            self.camera_display_snapshot=None;self.camera_canvas.delete('image');self.camera_inspection.clear();return
        frame,data,at=obs
        detection_at=(data or {}).get('detection_frame_at',at)
        from .camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
        detection_age_limit=MEASUREMENT_MAX_AGE_SECONDS if inspection else 1.
        detection_fresh=detection_at is not None and 0<=now-detection_at<detection_age_limit
        if not detection_fresh:self.camera_display_poses.clear()
        canvas=self.camera_inspection.canvas if inspection else self.camera_canvas
        key=(id(frame),at,detection_at,detection_fresh,inspection,self.camera_view_key(),self.catalog.revision,
             canvas.winfo_width(),canvas.winfo_height(),self.camera_inspection.station.get(),
             self.camera_inspection.product.get())
        # Capture is at most 10 FPS. Paint new frames immediately; repeat metadata
        # at 5 FPS instead of resizing the same frame on every 30 FPS 3-D tick.
        # Adoption/held poses have independent expiry and retain their full refresh.
        if (scheduled and not self.show_adoption.get() and not self.teaching_jig_hold_active()
                and not any(now-state[2]<self.camera_display_poses.duration for state in self.camera_display_poses.states.values())
                and key==getattr(self,'camera_paint_key',None)
                and 0<=now-getattr(self,'camera_painted_at',0)<.2):return
        if inspection:
            self.camera_inspection.update_frame(frame,self.camera_view_results(data) if detection_fresh else {},detection_fresh=detection_fresh,frame_at=at)
            self.camera_paint_key=key;self.camera_painted_at=now
            return
        self.camera_paint_key=key;self.camera_painted_at=now
        # Reuse only the drawing source while detection runs on a newer frame.
        # A completed empty result replaces it; video loss and a five-second age clear it.
        display_data=data;display_pending=False
        previous=getattr(self.camera,'observation',None)
        if not detection_fresh and not for_capture and previous and 0<=now-previous[2]<5 and (data or {}).get('status') not in ('paused','settings_changed','error'):
            display_data=previous[1];display_pending=True
        display_ready=detection_fresh or display_pending
        adopted=self.camera_adopted_positions(now)
        from .label_layout import LabelLayout
        from .inspection_geometry import anchors_for,project
        label_results={**self.camera_view_results(display_data or {}),**adopted}
        layout=LabelLayout(frame.shape,[r['selected'].get('outline_px',r['selected']['quad']) for r in label_results.values() if r.get('selected')])
        layout.protect([[20,15],[140,105]])
        if self.camera_inspection.reference.data:
            anchors,_=anchors_for(frame,{},{},{},self.profile,self.camera_inspection.reference.data,'리니어 조립','전체',self.workcell_preview,self.camera_inspection.endpoint.get())
            for anchor in anchors:
                layout.protect(project([[-35,-35,25],[35,-35,25],[35,35,25],[-35,35,25]],anchor,self.profile))
        if self.camera_all.get():
            from .vision import annotate_all
            results=self.camera_view_results(data) if detection_fresh else {}
            drawing=self.camera_view_results(display_data) if display_ready else {}
            if display_pending:drawing={k:{**v,'display_pending':True,'error':'이전 검출 · 갱신 중'} for k,v in drawing.items()}
            self.update_camera_overview(drawing)
            displayed=self.camera_display_results(drawing,now,raw=for_capture)
            shown_catalog={key:{**config,'name':('채택 · ' if key in adopted else '')+config['name']} for key,config in self.catalog.items.items()}
            shown=annotate_all(frame,{key:{**value,'outline_only':key in adopted} for key,value in displayed.items()},self.catalog.items,layout=layout)
            if adopted:shown=annotate_all(shown,{key:{**value,'adopted_view':True} for key,value in adopted.items()},shown_catalog,layout=layout)
            self.draw_camera_linear_centers(shown,layout=layout)
            metadata={'by_jig':deepcopy(results),'view':'all'}
            if self.show_adoption.get():metadata['adopted_by_jig']=adopted
            self.camera_display_snapshot={'raw_image':frame.copy(),'base_image':shown,'result':metadata,'at':at,'detection_at':data.get('detection_frame_at',at) if data else None,'jig_id':None}
            self.draw_roi();return
        live_result=self.camera_view_result(data) if detection_fresh else None
        result=self.camera_view_result(display_data) if display_ready else None
        if result and display_pending:result={**result,'display_pending':True,'error':'이전 검출 · 갱신 중'}
        if result:result=self.camera_display_results({self.active_jig:result},now,raw=for_capture).get(self.active_jig)
        if self.active_jig in adopted:result={**adopted[self.active_jig],'live_view':True,'adopted_view':True}
        if result and not result.get('live_view') and self.teaching_jig_hold_active():result=self.camera_results(now).get(self.active_jig,{'selected':None,'candidates':[],'status':'teaching_hold_empty'})
        import cv2
        from .vision import annotate,annotate_all,projected_jig_axes
        shown_result=result
        if result and result.get('selected'):
            raw=result['selected'];item=deepcopy(raw) if result.get('live_view') else self.display_poses.update(self.active_jig,raw,now,frozen=result.get('pose_frozen',False) or result.get('teaching_held',False))
            if item.get('metric'):
                config=self.catalog.items[self.active_jig];profile=support_plane_profile(self.profile,config)
                item['axes_px']=projected_jig_axes(item,profile,detection_plane_z(profile,self.catalog.mesh(self.active_jig)))
            shown_result={**result,'selected':item,'candidates':[item if c.get('quad')==raw.get('quad') else c for c in result.get('candidates',[])]}
        shown=annotate(frame,shown_result,layout=layout) if shown_result else frame
        if self.active_jig in adopted:
            config=self.catalog.items[self.active_jig]
            live_layer=annotate(frame,{**live_result,'outline_only':True},legend=False,layout=layout) if live_result else frame
            shown=annotate_all(live_layer,{self.active_jig:{**shown_result,'adopted_view':True}},{self.active_jig:{**config,'name':'채택 · '+config['name']}},layout=layout)
        if shown is frame:shown=frame.copy()
        self.draw_camera_linear_centers(shown,layout=layout)
        metadata=deepcopy(result if self.active_jig in adopted else live_result)
        if self.show_adoption.get():metadata={**(metadata or {}),'adopted_by_jig':adopted,'live_result':deepcopy(live_result)}
        self.camera_display_snapshot={'raw_image':frame.copy(),'base_image':shown.copy(),'result':metadata,'at':at,'detection_at':data.get('detection_frame_at',at) if data else None,'jig_id':self.active_jig}
        self.draw_roi()

    def draw_camera_linear_centers(self,image,*,layout=None):
        from .label_layout import draw_linear_centers
        panel=self.camera_inspection
        self.camera_linear_centers=draw_linear_centers(image,self.profile,panel.reference.data,self.workcell_preview,panel.endpoint.get(),layout=layout) if panel.reference.data else []

    def require_remote(self):
        if not self.remote or self.remote.error:raise ValueError('Pi 연결 복구 대기 중입니다. 자동 재연결 후 다시 실행하세요.'+((' · '+self.remote.error) if self.remote else ''))
        return self.remote
    def sync_remote_episode(self,doc):
        # Pi is authoritative at startup. Editing creates a local draft; only
        # the explicit export transaction changes the Pi's stored episodes.
        return
    def poll_remote_plan(self):
        task=self.remote_plan_job
        if not task or not task[0].done():return
        self.remote_plan_job=None;job,request,episode_id=task
        try:
            data=job.result()
            if episode_id!=self.episode['id']:raise ValueError('계산 중 에피소드가 바뀌어 실행을 취소했습니다.')
            if data['calibration_sha256']!=self.calibration.sha256 or data['bundle_hash']!=self.require_remote().bundle_hash:raise ValueError('Pi 계산 기준이 변경됐습니다. 다시 실행하세요.')
            self.begin_execution(*request,_remote_plan=data['plan'])
        except Exception as exc:self.set_pose_frozen(False);self.notice(str(exc),True)
    def poll(self):
        if self.page=='devices' and self.workspace_visible:self.dual_monitor.update()
        if self.closed:return
        self.ui_heartbeat=time.monotonic()
        if getattr(getattr(self.workspace_manager,'safe_shutdown',None),'busy',False):
            if self.session and hasattr(self.session,'heartbeat'):self.session.heartbeat=time.monotonic()
            self.job=self.root.after(80,self.poll);return
        try:
            self.settings.poll();self.poll_remote_plan();self.poll_extended();self.poll_linear_state();self.poll_arm_selection()
            if self.remote_mode and self.remote and self.remote.error:self.device_target.set('장치: Pi · 자동 재연결 대기')
            if self.session:
                if hasattr(self.session,'heartbeat'):self.session.heartbeat=time.monotonic()
                newest_sample=None
                while True:
                    try:kind,value=self.session.events.get_nowait()
                    except queue.Empty:break
                    if kind=='sample':newest_sample=value
                    elif kind=='error':self.notice(value,True)
                    elif kind=='notice':
                        self.notice(value)
                    elif kind=='device_notice':self.device_notice(value)
                    elif kind=='motion':
                        self.last_motion_stop=value['detail'] if value.get('paused') else None
                        self.notice(value['detail'])
                if newest_sample is not None:
                    self.latest=newest_sample
                    for n,label in zip(JOINTS,LABELS):
                        h=newest_sample.telemetry[n]
                        text=f'{label} · 현재 {newest_sample.ticks[n]}'
                        if self.current_vars[n].get()!=text:self.current_vars[n].set(text)
                connected=self.session.running and self.latest and self.latest.fresh()
                if connected:
                    on=any(h['torque'] for h in self.latest.telemetry.values())
                    self.connection.set(('실물 실행 · ' if getattr(self.session,'state','')=='MOVING' else '현재 자세 유지 · ' if getattr(self.session,'state','')=='HOLD' else '리더 따라가기 · ' if getattr(self.session,'state','')=='FOLLOW' else '시작 확인 · ' if getattr(self.session,'state','')=='ACTIVATING' else '읽기 연결 · ')+('토크 ON' if on else '토크 OFF')+('' if self.latest.calibration_matches else ' · 영점 불일치'))
                    if isinstance(self.session,MotionSession) and self.session.grip_contact.hold_tick is not None:self.connection.set(self.connection.get()+' · 집게 접촉 유지')
                    if self.connect_btn.cget('text')!='연결 해제':self.connect_btn.configure(text='연결 해제')
                    if isinstance(self.session,DemoSession):self.connection.set('데모 · USB 사용 안 함')
                elif not self.session.running:
                    self.connection.set('오류로 연결 종료' if self.session.error else '연결 종료')
                    if self.connect_btn.cget('text')!='팔로워 연결':self.connect_btn.configure(text='팔로워 연결')
                    error_key=(id(self.session),self.session.error)
                    if self.session.error and error_key!=getattr(self,'last_reported_session_error',None):self.notice(self.session.error,True);self.last_reported_session_error=error_key
                else:self.connection.set('현재값 수신 대기')
            if self.second_editor:self.second_editor.poll()
            self.poll_leader_status();self.poll_leader_assist()
            self.check_pending_execution()
            if self.inspection_run:self.inspection_run.poll()
            self.set_pose_frozen((self.live_adjust.owner is not None and not self.pending_execution and not self.camera_task) or bool(self.remote_plan_job) or bool(self.inspection_run and self.inspection_run.busy) or self.playing or bool(isinstance(self.session,MotionSession) and self.session.running and self.session.program_active.is_set()))
            self.update_live_jig_comparison();self.update_jig_pause_button()
            fresh=bool(not calibration_pending(self.profile) and self.session and self.session.running and self.latest and self.latest.fresh() and self.latest.calibration_matches)
            self.capture_btn.state(['!disabled'] if fresh and not self.safe_editing else ['disabled'])
            selected_step=next((s for s in self.episode['steps'] if s['id']==self.selected),None)
            self.update_step_btn.state(['!disabled'] if selected_step and not selected_step.get('safe_boundary') and not self.safe_editing else ['disabled'])
            state=getattr(self.session,'state','READ_ONLY');active=bool(self.session and self.session.running)
            self.delete_episode_btn.state(['!disabled'] if not self.episode_delete_busy() and any(doc['id']==self.episode['id'] for _,doc in self.library_entries) else ['disabled'])
            for button,allowed in [(self.arm_btn,active and fresh and state=='READ_ONLY'),(self.hold_btn,active and state in ('HOLD','MOVING','FOLLOW')),(self.release_btn,self.torque_release_available()),(self.move_btn,active and fresh and (self.live_adjust.owner is self or state=='HOLD' and not self.pending_execution)),(self.execute_btn,active and fresh and state=='HOLD' and not self.pending_execution and bool(self.episode['steps'])),(self.execute_taught_btn,active and fresh and state=='HOLD' and not self.pending_execution and not self.safe_entry and bool(self.episode['steps']))]:button.state(['!disabled'] if allowed else ['disabled'])
            blocked_preview=bool(self.remote_plan_job or self.pending_execution or self.safe_entry or state in ('MOVING','FOLLOW','ACTIVATING'))
            self.motion_speed_choice.configure(state='disabled' if blocked_preview or getattr(self.session,'command_pending',None) and self.session.command_pending.is_set() else 'readonly')
            for widget in (self.pause_btn,self.preview_reset_btn,self.timeline):widget.state(['disabled'] if blocked_preview or not self.transport.targets else ['!disabled'])
            self.connect_btn.state(['disabled'] if self.settings.worker and self.settings.worker.running else ['!disabled'])
            leader_running=bool(self.leader_session and self.leader_session.running)
            leader_ready=bool(leader_running and self.leader_session.latest and self.leader_session.latest.fresh())
            follow_text='지그 확인 · 따라가기 준비 중' if (self.camera_task or {}).get('kind')=='follow' else '리더 따라가기' if leader_ready or state=='FOLLOW' else '리더 연결 중' if leader_running else '리더 다시 연결'
            if self.follow_btn.cget('text')!=follow_text:self.follow_btn.configure(text=follow_text)
            self.follow_btn.state(['!disabled'] if not self.camera_task and active and fresh and state=='HOLD' and self.profile.get('mode')=='leader' and (not leader_running or leader_ready) else ['disabled'])
            if self.camera:
                if self.camera_sleeping:self.camera_status.set('카메라 꺼짐 · 필요할 때 연결합니다.')
                elif getattr(self.camera,'recovering',False):self.camera_status.set('Pi 영상 재수신 중 · 로봇 연결은 유지합니다.')
                elif self.camera.error:self.camera_status.set(self.camera.error)
                elif self.camera_view_observation() and time.monotonic()-self.camera_view_observation()[2]<1:
                    frame,data,at=self.camera_view_observation();h,w=frame.shape[:2];result=self.camera_view_result(data)
                    if result and not result.get('live_view') and self.teaching_jig_hold_active():result=self.camera_results().get(self.active_jig,{'selected':None,'candidates':[],'error':'자동 갱신 정지 · 지그 다시 읽기 필요'})
                    adopted=self.camera_adopted_positions(time.monotonic())
                    if not self.camera_all.get() and self.active_jig in adopted:result={**adopted[self.active_jig],'adopted_view':True}
                    info=f'실시간 {w}×{h} · 원본 영상'
                    if not getattr(self.camera,'processing_enabled',True):info+=' · 검출 정지 · 영상 최대 5fps' if self.camera.preview_fps==5 else ' · 사진 수집 중'
                    if data.get('preview_detection_age_s',0)>.5:info+=f" · 검출 결과 {data['preview_detection_age_s']:.1f}초 전"
                    if self.camera_all.get():
                        results=self.camera_view_results(data);self.update_camera_overview(results)
                        count=sum(bool(r.get('selected')) and not r.get('pose_held',False) for r in results.values())
                        info+=f'  |  전체 지그 감지 {count}/{len(self.catalog.items)} · 오른쪽 목록에서 상태 확인'
                    elif result and result.get('selected'):
                        chosen=result['selected'];x,y=chosen['center_px'];metric=chosen['metric']
                        if result.get('adopted_view'):info+='  |  채택 위치 · + 중심 표시'
                        elif result.get('live_view'):info+='  |  최신 검출 · + 중심 표시'
                        elif result.get('teaching_held'):info+='  |  자동 갱신 정지 · 티칭 위치 유지'
                        else:info+=f"  |  {'실행 중 고정' if result.get('pose_frozen') else '위치 유지' if result.get('pose_held') else '새로 측정'} {result.get('hold_remaining_s',0):.1f}s · + 중심 표시 · {'연속 감지로 확정' if chosen.get('temporal_match') else '외곽·구멍 일치'}"
                        if metric:
                            dx,dy=display_position(metric['center_xy_mm']);heading=display_heading(metric['yaw_deg'],metric.get('symmetry_deg',90))
                            info+=f"\n표시 X→ {dx:.1f}, Y↑ {dy:.1f} mm · 방향 {heading:.1f}° ({metric.get('symmetry_deg',90)}° 대칭) · 원점=로봇 베이스 · 카메라 추정값"
                        if chosen.get('raised_features_verified'):info+='\nSTL 높이를 반영한 윗면 돌출 모서리 확인'
                        if chosen.get('orientation_verified'):info+='\nA 지그 원형 구멍으로 앞뒤 방향 확인'
                        if chosen.get('grid_verified'):info+='\n내부 교차점 2개와 외곽 방향 일치'+('' if chosen.get('orientation_verified') else ' · 앞뒤 180° 대칭')
                        if chosen.get('partial_visible'):info+='\n일부가 화면 밖에 있습니다. 보이는 네 변과 STL 크기로 중심을 계산했습니다.'
                    elif result:info+='  |  '+(f"획득 {result['stable_candidate_seconds']:.1f}/{result.get('acquisition_seconds',3.):g}초 · 새 관측 {result.get('stable_candidate_samples',0)}회" if 'stable_candidate_seconds' in result else '여러 후보 · 영역을 좁혀 주세요' if result.get('status')=='ambiguous' else result.get('error','지그 형상 미확인'))
                    self.camera_status.set(info)
                elif not self.camera.running:self.camera_status.set('카메라 연결 해제됨')
                else:self.camera_status.set('최신 카메라 영상 대기')
            if self.camera_all.get():
                view=self.camera_view_observation()
                if not view or self.camera_sleeping or getattr(self.camera,'recovering',False) or getattr(self.camera,'error',None) or time.monotonic()-view[2]>=1:
                    self.update_camera_overview({});self.camera_display_snapshot=None;self.camera_canvas.delete('image')
            if self.show_adoption.get():self.update_adoption_caption()
            self.live_adjust.poll()
        except Exception as exc:self.notice('상태 갱신 오류: '+str(exc),True)
        if self.inspection_run and self.inspection_run.busy:
            for button in (self.arm_btn,self.follow_btn,self.move_btn,self.execute_btn,self.execute_taught_btn):button.state(['disabled'])
        from .pi_execution import active_execution
        if active_execution(self):
            for button in (self.connect_btn,self.arm_btn,self.follow_btn,self.move_btn,self.execute_btn,self.execute_taught_btn):button.state(['disabled'])
            self.hold_btn.state(['!disabled'])
        if self.camera_only:
            self.connection.set('카메라 전용 · 모터 연결 차단')
            for button in (self.connect_btn,self.arm_btn,self.hold_btn,self.release_btn,self.follow_btn,self.execute_btn,self.execute_taught_btn):button.state(['disabled'])
        self.job=self.root.after(80,self.poll)
    def close(self):
        if self.closed:return
        if self.workspace_manager and not self.workspace_manager.closing:
            self.workspace_manager.close();return
        if self.inspection_run:self.inspection_run.cancel('화면 종료',hold=True)
        try:self.live_adjust.stop(halt=False)
        except Exception as exc:self.notice('종료 중 화면 정리 실패: '+str(exc),True)
        self.cancel_space_key();self.cancel_jig_key();self.closed=True
        if self.linear_state_query:self.linear_state_query.close()
        if getattr(self,'startup_job',None):self.root.after_cancel(self.startup_job);self.startup_job=None
        if self.job:self.root.after_cancel(self.job)
        if self.render_job:self.root.after_cancel(self.render_job)
        if self.session:self.session.close()
        if self.camera:self.camera.close()
        self.camera_inspection.worker.close()
        if self.leader_session:self.leader_session.close()
        self.settings.close()
        if self.renderer:self.renderer.close()
        self.connection.set('장치 연결 정리 중')
        self.finish_close()
    def finish_close(self):
        linear=self.workspace_manager.linear_state_query if self.workspace_manager else self.linear_state_query
        if linear and linear.is_alive():
            self.root.after(50,self.finish_close);return
        # Keep the event loop alive until VideoCapture releases in its owner thread.
        if (self.session and self.session.running) or (self.camera and self.camera.running) or (self.leader_session and self.leader_session.running) or (self.settings.worker and self.settings.worker.running):
            self.root.after(50,self.finish_close);return
        # Unmap before child destruction so bright empty panels are never exposed.
        if self.remote:self.remote.close(disconnect=False)
        if self.workspace_manager:
            self.workspace_manager.finished_close(self);return
        self.root.withdraw();self.root.update_idletasks();self.root.destroy()
