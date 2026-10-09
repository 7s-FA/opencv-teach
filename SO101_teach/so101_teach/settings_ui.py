"""In-window configuration pages; no native file dialogs or additional windows."""
from copy import deepcopy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json,time,tkinter as tk
from tkinter import ttk
from .ui_scroll import AutoScrollbar
from .domain import ROOT,JOINTS,LABELS,Calibration,atomic_json,read_json
from .motion import FOLLOW_START_SPEEDS,DEFAULT_FOLLOW_START_RATE
from .configuration import ProfileLibrary,model_tcp,tcp_matrix,TCP_PRESETS,tcp_label
from .height_reference import support_height,rim_height,base_z_from_height

class SettingsPanel:
    def __init__(self,app,parent):
        self.model_views={};self.model_renderer=None;self.model_dirty={};self.model_file_check_at={};self.models_closed=False;self.cad_tcp=model_tcp()
        self.app=app;self.scroll_canvases={};self.vars={};self.worker=None;self.pool=ThreadPoolExecutor(max_workers=1);self.job=None;self.library=ProfileLibrary(app.data_dir)
        if not self.library.items:self.library.save(app.profile['name'],app.profile,'default')
        self.active_profile_key=next((k for k,v in self.library.items.items() if v.get('port')==app.profile['port'] and v.get('calibration_file')==app.profile['calibration_file']),None)
        self.tabs=ttk.Notebook(parent);self.tabs.pack(fill='both',expand=True);self.pages={}
        for key,title in [('robot','로봇'),('calibration','3점 각도 보정'),('jigs','지그 관리'),('camera','카메라 보정'),('tcp','집게 끝 TCP'),('pi','라즈베리파이 연결'),('help','사용 안내')]:
            outer=ttk.Frame(self.tabs);self.tabs.add(outer,text=title);self.pages[key]=outer
        self.build_robot();
        from .calibration_ui import CalibrationPanel
        self.calibration_panel=CalibrationPanel(self)
        self.build_jigs();self.build_camera();self.build_tcp();self.build_help()
        from .pi_settings_ui import PiSettingsPanel
        self.pi_panel=PiSettingsPanel(self)
        from .episode_transfer_ui import EpisodeTransferPanel
        self.episode_transfer_panel=EpisodeTransferPanel(self)
        for key in ('jigs','tcp'):
            prefixes=('jig_',) if key=='jigs' else ('tcp_',)
            for name,var in self.vars.items():
                if name.startswith(prefixes):var.trace_add('write',lambda *args,k=key:self.mark_model_dirty(k))
            self.mark_model_dirty(key)
        self.model_job=app.root.after(33,self.poll_models)
        app.root.bind_all('<Button-4>',lambda e:self.scroll_page(-2),add='+');app.root.bind_all('<Button-5>',lambda e:self.scroll_page(2),add='+')
    def scroll_page(self,amount):
        if self.app.workspace_visible and self.app.page=='settings':
            canvas=self.scroll_canvases.get(self.tabs.select())
            if canvas:
                first,last=canvas.yview()
                if last-first<.99999:canvas.yview_scroll(amount,'units')
                return 'break'
    def bind_list_wheel(self,widget):
        def scroll(amount):widget.yview_scroll(amount,'units');return 'break'
        widget.bind('<Button-4>',lambda e:scroll(-3))
        widget.bind('<Button-5>',lambda e:scroll(3))
        widget.bind('<MouseWheel>',lambda e:scroll(-max(1,abs(int(e.delta/120))) if e.delta>0 else max(1,abs(int(e.delta/120)))))
    def form(self,key):
        outer=self.pages[key];outer.columnconfigure(0,weight=1);outer.rowconfigure(0,weight=1)
        canvas=tk.Canvas(outer,bg='white',highlightthickness=0);self.scroll_canvases[str(outer)]=canvas;canvas.grid(row=0,column=0,sticky='nsew');bar=AutoScrollbar(outer,command=canvas.yview);bar.grid(row=0,column=1,sticky='ns');canvas.configure(yscrollcommand=bar.set)
        body=ttk.Frame(canvas,padding=18,style='Card.TFrame');win=canvas.create_window(0,0,window=body,anchor='nw');body.columnconfigure(1,weight=1)
        def resize_form(event):
            canvas.configure(scrollregion=canvas.bbox('all'))
            if body.winfo_reqheight()<=canvas.winfo_height():canvas.yview_moveto(0)
            for widget in body.winfo_children():
                if isinstance(widget,ttk.Label) and int(widget.grid_info().get('columnspan',1))>=3:
                    widget.configure(wraplength=max(160,min(960,body.winfo_width()-36)))
        body.bind('<Configure>',resize_form);canvas.bind('<Configure>',lambda e:(canvas.itemconfigure(win,width=e.width),resize_form(e)))
        body.inspection_form=key in ('tcp','jigs')
        if body.inspection_form:
            from .inspection_ui import ModelView
            outer.columnconfigure(0,weight=3);outer.columnconfigure(2,weight=3,minsize=350)
            pane=ModelView(outer,'tcp' if key=='tcp' else 'jig');pane.grid(row=0,column=2,sticky='nsew',padx=(8,0));self.model_views[key]=pane
        return body
    def label(self,p,row,text):ttk.Label(p,text=text,wraplength=420 if p.inspection_form else 850).grid(row=row,column=0,columnspan=3,sticky='w',pady=7)
    def field(self,p,row,label,key,value,choices=None):
        ttk.Label(p,text=label,wraplength=135 if p.inspection_form else 250).grid(row=row,column=0,sticky='w',padx=(0,12),pady=5)
        var=tk.StringVar(value=str(value));self.vars[key]=var
        widget=ttk.Combobox(p,textvariable=var,values=choices,state='readonly') if choices is not None else ttk.Entry(p,textvariable=var)
        widget.grid(row=row,column=1,sticky='ew',pady=5);return widget
    def button(self,p,row,text,command,column=2):
        w=self.app.button(p,text,command);w.grid(row=row,column=column,sticky='w',padx=5,pady=5);return w
    def value(self,key):return self.vars[key].get().strip()
    def choose(self,key,extensions=('.json',),directory=False):
        previous=self.tabs.select();page=ttk.Frame(self.tabs,padding=16);self.tabs.add(page,text='파일 선택');self.tabs.select(page)
        path=tk.StringVar(value=str(Path(self.value(key)).expanduser().parent if Path(self.value(key)).expanduser().is_file() else Path(self.value(key) or Path.home()).expanduser()))
        entry=ttk.Entry(page,textvariable=path);entry.pack(fill='x')
        list_frame=ttk.Frame(page);list_frame.pack(fill='both',expand=True,pady=10);list_frame.columnconfigure(0,weight=1);list_frame.rowconfigure(0,weight=1)
        listing=tk.Listbox(list_frame,font=('Noto Sans CJK KR',11),height=16);listing.grid(row=0,column=0,sticky='nsew')
        vertical=AutoScrollbar(list_frame,orient='vertical',command=listing.yview);vertical.grid(row=0,column=1,sticky='ns')
        horizontal=AutoScrollbar(list_frame,orient='horizontal',command=listing.xview);horizontal.grid(row=1,column=0,sticky='ew')
        listing.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set);self.bind_list_wheel(listing);items=[]
        def refresh():
            folder=Path(path.get()).expanduser().resolve()
            if not folder.is_dir():raise ValueError('폴더 경로를 입력하세요.')
            path.set(str(folder));items[:]=[folder.parent]+sorted((p for p in folder.iterdir() if p.is_dir() or not directory and p.suffix.lower() in extensions),key=lambda p:(not p.is_dir(),p.name.lower()))
            listing.delete(0,'end')
            for p in items:listing.insert('end',('..' if p==folder.parent else p.name)+(' /' if p.is_dir() else ''))
        def close():self.tabs.select(previous);self.tabs.forget(page);page.destroy()
        def select():
            selected=listing.curselection()
            if not selected:return
            p=items[selected[0]]
            if p.is_dir():path.set(str(p));refresh()
            else:self.vars[key].set(str(p));close()
        listing.bind('<Double-1>',lambda e:self.app.guard(select));entry.bind('<Return>',lambda e:self.app.guard(refresh))
        row=ttk.Frame(page);row.pack(fill='x')
        self.app.button(row,'열기',select).pack(side='left');self.app.button(row,'닫기',close).pack(side='right')
        if directory:self.app.button(row,'이 폴더 선택',lambda:(self.vars[key].set(path.get()),close())).pack(side='left')
        self.app.guard(refresh)
    def require_idle(self):
        a=self.app
        if getattr(a,'remote_plan_job',None) or a.playing or a.pending_execution or a.session and a.session.running or getattr(a,'leader_session',None) and a.leader_session.running or self.worker and self.worker.running:raise ValueError('로봇 연결과 실행을 종료한 뒤 설정을 변경하세요.')
    def calculation_busy(self):
        a=self.app
        return bool(a.playing or a.pending_execution or a.camera_task or a.safe_entry or a.detector.frozen or getattr(a.session,'state','') in ('ACTIVATING','MOVING','FOLLOW'))
    def require_calculation_idle(self):
        if self.calculation_busy():raise ValueError('측정·실행이 끝난 뒤 계산 기준을 적용하세요. 입력값 편집은 가능합니다.')
    def save_profile(self):
        self.require_idle();a=self.app;profile=deepcopy(a.profile)
        from .gripper_contact import GripperContact
        threshold=int(self.value('grip_contact_load'));GripperContact(threshold);profile['grip_contact_load']=threshold
        profile['follow_start_rate_ticks_s']=FOLLOW_START_SPEEDS[self.value('follow_start_speed')]
        path,cal=self.library.copy_calibration(self.value('follower_json'));profile=self.library.reference_for(profile,cal)
        profile.update(name=self.value('robot_name'),port=self.value('follower_port'),calibration_file=path,mode={'팔로워 단독':'follower','리더 + 팔로워':'leader','데모':'demo'}[self.value('mode')])
        if profile['mode']=='leader':
            leader_path,leader_cal=self.library.copy_calibration(self.value('leader_json'),'leader')
            if not a.remote_mode and Path(self.value('leader_port')).resolve()==Path(profile['port']).resolve():raise ValueError('리더와 팔로워 포트는 달라야 합니다.')
            profile.update(leader={'port':self.value('leader_port'),'calibration_file':leader_path,'angle_mapping_sha256':leader_cal.angle_mapping.sha256 if leader_cal.angle_mapping else None})
        self.active_profile_key=self.library.save(profile['name'],profile,self.active_profile_key);a.install_profile(profile);self.refresh_profiles();a.notice('로봇 설정 적용 · 기존 에피소드는 변환하지 않습니다.')
    def refresh_profiles(self):
        self.profile_keys=list(self.library.items);self.profile_choice.configure(values=[self.library.items[k]['name'] for k in self.profile_keys])
        if self.active_profile_key in self.profile_keys:self.profile_choice.current(self.profile_keys.index(self.active_profile_key))
        self.app.refresh_arm_selector()
    def load_profile(self):
        i=self.profile_choice.current()
        if i<0:raise ValueError('저장된 로봇을 선택하세요.')
        if self.profile_keys[i]==self.active_profile_key:
            self.fill_robot();return
        if self.app.workspace_manager:
            from .arm_workspace import arm_id
            target=self.library.items[self.profile_keys[i]]
            if arm_id(target)!=arm_id(self.app.profile):self.app.workspace_manager.select(arm_id(target));return
        self.require_idle()
        self.active_profile_key=self.profile_keys[i];self.app.install_profile(deepcopy(self.library.items[self.active_profile_key]));self.fill_robot()
    def select_profile(self,event=None):
        previous=self.active_profile_key
        try:self.load_profile()
        except Exception:
            self.active_profile_key=previous
            if previous in self.profile_keys:self.profile_choice.current(self.profile_keys.index(previous))
            else:self.vars['profile_choice'].set('')
            raise
    def refresh_current_settings(self):
        self.fill_robot();p=self.app.profile
        if self.calibration_panel.state not in ('MIDPOINT','ANGLES','RANGE','VERIFY'):self.calibration_panel.sync_role()
        for key,value in [('cam_source',p['camera']['source']),('cam_width',p['camera']['width']),('cam_height',p['camera']['height'])]:self.vars[key].set(str(value))
        t=p['tcp'];self.vars['tcp_mode'].set(tcp_label(t))
        for i,axis in enumerate('XYZ'):self.vars['tcp_xyz_'+axis].set(f"{t['xyz_mm'][i]:.6f}")
        for i,axis in enumerate(('roll','pitch','yaw')):self.vars['tcp_rpy_'+axis].set(f"{t['rpy_deg'][i]:.6f}")
        self.update_tcp_inputs()
        self.restore_calibration_photos()
    def new_robot(self):
        self.active_profile_key=None;self.vars['robot_name'].set('새 SO-101');self.vars['follower_json'].set('');self.vars['leader_json'].set('');self.vars['mode'].set('리더 + 팔로워');self.app.notice('새 로봇의 포트와 보정 JSON을 선택하거나 새 보정을 진행하세요.')
    def fill_robot(self):
        p=self.app.profile
        for k,v in {'robot_name':p['name'],'follower_port':p['port'],'follower_json':str(self.app.data_dir/p['calibration_file']), 'leader_port':p.get('leader',{}).get('port',''),'leader_json':str(self.app.data_dir/p['leader']['calibration_file']) if p.get('leader',{}).get('calibration_file') else '', 'mode':{'follower':'팔로워 단독','leader':'리더 + 팔로워','demo':'데모'}.get(p.get('mode'),'리더 + 팔로워')}.items():self.vars[k].set(v)
        self.vars['grip_contact_load'].set(str(p.get('grip_contact_load',80)))
        self.vars['follow_start_speed'].set(next((k for k,v in FOLLOW_START_SPEEDS.items() if v==p.get('follow_start_rate_ticks_s',DEFAULT_FOLLOW_START_RATE)),'빠르게'))
    def device_ports(self,role):
        if self.app.remote_mode and role=='follower':
            link=self.app.remote
            return list(link.health['devices']['serial']) if link and hasattr(link,'health') else []
        return sorted(str(x) for x in Path('/dev/serial/by-id').glob('*'))
    def refresh_device_ports(self):
        self.follower_port_choice.configure(values=self.device_ports('follower'))
        self.leader_port_choice.configure(values=['',*self.device_ports('leader')])
        if not self.worker or not self.worker.running:self.calibration_panel.sync_role()
        if hasattr(self,'camera_source_choice'):
            self.set_camera_choices(getattr(self.app.remote,'health',{}).get('devices',{}).get('camera_details',[]) if self.app.remote_mode else [])
    def build_robot(self):
        p=self.form('robot');self.label(p,0,'로봇 설정 · 팔별 보정 파일을 구분합니다. 연결만으로 토크가 켜지지 않습니다.')
        self.profile_choice=self.field(p,1,'저장된 로봇','profile_choice','',[])
        self.profile_choice.bind('<<ComboboxSelected>>',lambda event:self.app.guard(self.select_profile))
        ports=sorted(str(x) for x in Path('/dev/serial/by-id').glob('*'));a=self.app
        self.field(p,2,'로봇 이름','robot_name',a.profile['name']);self.button(p,2,'새 로봇 등록',self.new_robot);self.field(p,3,'실행 모드','mode','리더 + 팔로워',['리더 + 팔로워','팔로워 단독','데모'])
        self.follower_port_choice=self.field(p,4,'팔로워 포트 (Pi 모드: Pi)','follower_port',a.profile['port'],self.device_ports('follower') or [a.profile['port']]);self.follower_port_choice.configure(state='normal');self.field(p,5,'팔로워 보정 JSON','follower_json',str(a.data_dir/a.profile['calibration_file']));self.button(p,5,'파일 선택',lambda:self.choose('follower_json'))
        self.leader_port_choice=self.field(p,6,'리더 포트 (항상 PC)','leader_port','',['',*ports]);self.leader_port_choice.configure(state='normal');self.field(p,7,'리더 보정 JSON','leader_json','');self.button(p,7,'파일 선택',lambda:self.choose('leader_json'))
        self.field(p,8,'집게 접촉 부하 (raw)','grip_contact_load',a.profile.get('grip_contact_load',80))
        self.grip_load_percent=tk.StringVar()
        def show_load_percent(*_):
            try:
                value=int(self.vars['grip_contact_load'].get())
                text=f'약 {value/1023*100:.1f}% · 최대 1023 (100%)' if 1<=value<=1023 else '입력 범위: 1~1023 (최대 100%)'
            except ValueError:text='입력 범위: 1~1023 (최대 100%)'
            self.grip_load_percent.set(text)
        self.vars['grip_contact_load'].trace_add('write',show_load_percent);show_load_percent()
        ttk.Label(p,textvariable=self.grip_load_percent,style='Small.TLabel').grid(row=8,column=2,sticky='w',padx=(8,0),pady=5)
        self.label(p,9,'표시 부하 기준: 1023을 100%로 환산합니다. 실제 모터 최대 힘·토크의 비율을 뜻하지는 않습니다.\n'
            '예: 50: 4.9% · 80: 7.8% · 100: 9.8% · 200: 19.6%. 작은 값일수록 작은 부하에서 접촉으로 판단합니다.\n'
            '부하가 약 6%이면 설정 50(약 5%)에서는 접촉 후보가 되고, 설정 80(약 8%)에서는 기준 미달입니다.\n'
            '닫는 중 부하와 위치 정체가 0.12초·3회 이상 확인되면 더 닫지 않고 유지합니다. 최대 100%는 입력 상한이며 권장값이 아닙니다.')
        robot_actions=ttk.Frame(p,style='Card.TFrame');robot_actions.grid(row=10,column=0,columnspan=3,sticky='w',pady=6)
        self.app.button(robot_actions,'로봇 설정 저장·적용',self.save_profile,True).pack(side='left',padx=(0,8))
        self.app.button(robot_actions,'3점 각도 보정 열기',lambda:self.tabs.select(self.pages['calibration'])).pack(side='left')
        from .leader_assist_ui import CHOICES
        self.leader_assist_choice=self.field(p,12,'리더 무게 보조','leader_assist',a.preferences.get('leader_gravity_assist','약하게'),CHOICES)
        a.leader_assist_choice=self.vars['leader_assist']
        self.button(p,12,'보조 설정 적용',a.save_leader_assist)
        a.leader_assist_status=tk.StringVar(value='대기 · 따라가기에서 약하게')
        ttk.Label(p,textvariable=a.leader_assist_status,style='Small.TLabel').grid(row=13,column=1,sticky='w',pady=4)
        self.label(p,14,'따라가기 중 어깨·팔꿈치의 무게를 일부 받쳐 줍니다. 처음에는 약하게 사용하세요.\n'
            '움직일 때는 보조를 줄이고, 느려지면 천천히 회복합니다.\n'
            '동작 정지·토크 해제·Esc로 보조도 해제합니다. 손을 놓아도 자세를 유지하는 기능은 아닙니다.')
        self.field(p,15,'최초 합류 속도','follow_start_speed','빠르게',list(FOLLOW_START_SPEEDS))
        self.label(p,16,'리더 따라가기 시작 시에만 적용합니다. 합류 후 조정 속도는 그대로 유지합니다.\n아주 느리게 100 · 느리게 200 · 보통 300 · 빠르게 400틱/초 · 로봇 설정 저장·적용으로 저장합니다.')
        self.refresh_profiles();self.fill_robot()
    def start_calibration(self,use_file=False):
        if self.app.camera_only:raise ValueError('카메라 전용 실행에서는 모터 보정을 시작하지 않습니다.')
        self.require_idle()
        if self.app.profile.get('mode')=='demo':raise ValueError('데모에서는 실물 보정을 시작하지 않습니다. 로봇 설정에서 실물 모드를 선택하세요.')
        from .calibration import CalibrationWorker
        role='leader' if self.value('cal_role')=='리더' else 'follower';port=self.value('cal_port');path=self.value(role+'_json')
        if not port:raise ValueError('보정할 팔의 USB 포트를 선택하세요.')
        if role=='follower' and port!=self.app.profile['port']:raise ValueError('선택한 로봇팔의 등록 포트에서 보정하세요.')
        other=self.value(('follower' if role=='leader' else 'leader')+'_port')
        if not self.app.remote_mode and other and Path(port).resolve()==Path(other).resolve():raise ValueError('리더와 팔로워의 보정 포트는 달라야 합니다. 보정할 팔의 USB 포트를 확인하세요.')
        cal=Calibration(path) if path and Path(path).is_file() else self.app.calibration;preset=self.value('cal_import') if use_file else None
        if use_file:
            imported=Calibration(preset)
            if self.app.profile.get('calibration_status')=='pending' and not imported.angle_mapping:
                raise ValueError('초기 연결용 파일은 모터에 적용할 수 없습니다. 보정 시작으로 새 3점 보정을 진행하세요.')
        self.vars[role+'_port'].set(port)
        target=self.app.data_dir/'calibration'/f'{role}-{time.time_ns()}.json';self.calibration_role=role
        from .arm_workspace import progress_path
        progress=progress_path(self.app.data_dir,role,self.app.profile)
        resume=read_json(progress) if not use_file and progress.exists() else None
        self.calibration_panel.last_sample=None;self.calibration_panel.state_changed('MIDPOINT')
        if self.app.remote_mode and role=='follower':
            from .remote_client import RemoteCalibrationWorker
            link=self.app.require_remote();link.sync(self.app);self.worker=RemoteCalibrationWorker(link,role,port,target,preset=preset)
        else:self.worker=CalibrationWorker(port,cal,target,preset=preset,progress_path=progress,resume=resume)
        self.worker.start()
    def cal_command(self,command):
        if not self.worker or not self.worker.running:raise ValueError('먼저 보정을 시작하세요.')
        self.worker.commands.put(command)
    def build_jigs(self):
        p=self.form('jigs');self.jig_choice=self.field(p,0,'등록된 지그','jig_pick','',[]);self.jig_choice.bind('<<ComboboxSelected>>',lambda e:self.app.guard(self.fill_jig));self.button(p,0,'새 지그',self.new_jig)
        self.field(p,1,'이름','jig_name','');self.field(p,2,'STL 파일 (선택)','jig_stl','');self.button(p,2,'파일 선택',lambda:self.choose('jig_stl',('.stl',)))
        self.field(p,3,'STL 단위','jig_unit','mm',['mm','cm','m']);self.field(p,4,'형태','jig_shape','도넛형',['직사각형','도넛형'])
        self.jig_method_keys={'윤곽 중심':'edges','색상+윤곽':'combined','외곽+교차점':'grid','외곽+교차점+원형':'grid','윤곽+돌출 모서리':'edges','색상+윤곽+돌출':'combined'}
        self.jig_method_choice=self.field(p,5,'검출 방식','jig_method','윤곽 중심',['색상+윤곽','윤곽 중심','외곽+교차점'])
        self.field(p,6,'STL 없을 때 가로 mm','jig_width',70);self.field(p,7,'STL 없을 때 세로 mm','jig_depth',70);self.field(p,8,'STL 없을 때 테두리 높이 mm','jig_rim',20)
        self.field(p,9,'받침 높이 mm (바닥=0)','jig_z',0)
        self.jig_info=tk.StringVar(value='STL을 넣으면 외곽 크기·높이·구멍을 자동 분석합니다.');ttk.Label(p,textvariable=self.jig_info,wraplength=380).grid(row=10,column=0,columnspan=3,sticky='w',pady=8)
        jig_actions=ttk.Frame(p,style='Card.TFrame');jig_actions.grid(row=11,column=0,columnspan=3,sticky='ew',pady=6)
        self.app.button(jig_actions,'지그 저장',self.save_jig,True).pack(side='left',padx=(0,8))
        self.app.button(jig_actions,'복사',self.copy_jig).pack(side='left')
        self.app.button(jig_actions,'삭제',self.delete_jig).pack(side='right')
        self.jig_detection_note=ttk.Label(p,text='외곽+교차점은 3×2 홈 또는 고정 지그가 있는 운반용 지그에 사용합니다. A 지그의 고정 원형 구멍으로 앞뒤를 확인하고, 다른 부품 내부는 검사하지 않습니다. 빈 팔레트의 윤곽+돌출 모서리는 검출 방식이고, 돌출부 확인은 영상에서 실제 확인된 상태입니다. 원형 기준을 못 찾으면 방향을 채택하지 않습니다.',wraplength=380)
        self.jig_detection_note.grid(row=12,column=0,columnspan=3,sticky='w',pady=7)
        self.refresh_jigs();self.fill_jig()
    def refresh_jigs(self):
        self.jig_keys=list(self.app.catalog.items);self.jig_choice.configure(values=[self.app.catalog.items[k]['name'] for k in self.jig_keys])
        if self.jig_keys:self.jig_choice.current(0)
    def fill_jig(self):
        i=self.jig_choice.current()
        if i<0:return
        d=self.app.catalog.items[self.jig_keys[i]];self.jig_id=d['id']
        for key,v in {'jig_name':d['name'],'jig_stl':d.get('stl') or '','jig_unit':d['unit'],'jig_shape':{'rectangle':'직사각형','ring':'도넛형'}[d['shape']],'jig_method':{'edges':'윤곽 중심','combined':'색상+윤곽','grid':'외곽+교차점'}[d['method']],'jig_z':f'{support_height(d,self.app.profile):g}'}.items():self.vars[key].set(v)
        m=self.app.catalog.mesh(self.jig_id)
        self.update_jig_method_names(m)
        for k,v in zip(('jig_width','jig_depth','jig_rim'),[*m['size_mm'][:2],rim_height(m)]):self.vars[k].set(str(v))
        model_info=f"고정 지그 {len(m['assembly']['fixtures'])}개 · 운반판 기준" if m.get('assembly') else f"내부 구멍 {len(m['holes'])}개"
        self.jig_info.set(f"STL 크기 {[round(v, 2) for v in m['size_mm']]} mm · {model_info}\n받침 {support_height(d,self.app.profile):g} mm · 감지면 {support_height(d,self.app.profile)+rim_height(m):g} mm (바닥 기준)")
    def update_jig_method_names(self,mesh=None):
        raised=self.value('jig_shape')=='도넛형' and bool((mesh or {}).get('raised_edges_mm'))
        direction=bool((mesh or {}).get('assembly',{}).get('orientation_hole'))
        names={'edges':'윤곽+돌출 모서리' if raised else '윤곽 중심','combined':'색상+윤곽+돌출' if raised else '색상+윤곽','grid':'외곽+교차점+원형' if direction else '외곽+교차점'}
        method=self.jig_method_keys.get(self.value('jig_method'),'edges')
        self.jig_method_choice.configure(values=[names[k] for k in ('combined','edges','grid')])
        if self.value('jig_method')!=names[method]:self.vars['jig_method'].set(names[method])
    def new_jig(self):
        self.jig_id=None;self.vars['jig_z'].set('0');self.vars['jig_name'].set('새 지그');self.vars['jig_stl'].set('');self.jig_info.set('STL을 선택하고 저장하세요.');self.update_jig_method_names()
    def save_jig(self):
        if self.app.workspace_manager:self.app.workspace_manager.require_shared_jig_idle(self.app)
        self.require_calculation_idle()
        a=self.app
        if a.pose_latch.frozen or a.pending_execution:raise ValueError('실행이 끝난 뒤 지그를 변경하세요.')
        old=a.catalog.items.get(self.jig_id,{})
        d=a.catalog.save({**deepcopy(old),'id':self.jig_id,'name':self.value('jig_name'),'stl':self.value('jig_stl'),'unit':self.value('jig_unit'),'shape':{'직사각형':'rectangle','도넛형':'ring'}[self.value('jig_shape')],'method':self.jig_method_keys[self.value('jig_method')],'support_height_mm':float(self.value('jig_z') or '0'),'roi':old.get('roi'),'manual_size_mm':[70,70] if self.value('jig_stl') else [float(self.value('jig_width')),float(self.value('jig_depth'))],'manual_rim_mm':20 if self.value('jig_stl') else float(self.value('jig_rim'))})
        self.jig_id=d['id'];a.catalog_changed();self.refresh_jigs();self.jig_choice.current(self.jig_keys.index(d['id']));self.fill_jig();a.notice('지그 저장 · 기존 스텝의 티칭 기준은 유지됩니다.')
    def copy_jig(self):
        if self.app.workspace_manager:self.app.workspace_manager.require_shared_jig_idle(self.app)
        self.require_calculation_idle()
        if self.jig_id:
            item=self.app.catalog.duplicate(self.jig_id);self.app.catalog_changed();self.refresh_jigs();self.jig_choice.current(self.jig_keys.index(item['id']));self.fill_jig()
    def delete_jig(self):
        if self.app.workspace_manager:self.app.workspace_manager.require_shared_jig_idle(self.app)
        self.require_calculation_idle()
        used={s.get('jig_id') for path in self.app.store.directory.glob('*.json') for s in read_json(path).get('steps',[])};used.update(s.get('jig_id') for s in self.app.episode['steps'])
        self.app.catalog.remove(self.jig_id,used);self.app.catalog_changed();self.refresh_jigs();self.fill_jig()
    def build_camera(self):
        p=self.form('camera');a=self.app
        self.camera_choices={};self.camera_devices_job=None
        self.camera_source_choice=self.field(p,0,'카메라 컬러 채널','cam_source',a.profile['camera']['source'],[]);self.camera_source_choice.configure(state='normal')
        self.camera_refresh_btn=self.button(p,0,'목록 새로고침',self.refresh_camera_devices)
        self.set_camera_choices(getattr(a.remote,'health',{}).get('devices',{}).get('camera_details',[]) if a.remote_mode else [])
        self.field(p,1,'너비 px','cam_width',a.profile['camera']['width']);self.field(p,2,'높이 px','cam_height',a.profile['camera']['height']);self.button(p,2,'장치 설정 적용',self.save_camera_device)
        default=ROOT/'data/camera_images';self.field(p,3,'체커보드 사진 폴더','photo_dir',default);self.button(p,3,'폴더 선택',lambda:self.choose('photo_dir',directory=True))
        self.field(p,4,'내부 코너 가로','board_cols',13);self.field(p,5,'내부 코너 세로','board_rows',9);self.field(p,6,'코너 간격 mm','board_mm',20)
        photo_actions=ttk.Frame(p,style='Card.TFrame');photo_actions.grid(row=7,column=0,columnspan=3,sticky='w',pady=6)
        for text,command in (('폴더 사진 불러오기',self.load_photos),('현재 사진 수집',self.capture_board),('선택 사진으로 보정 계산',self.calibrate_camera)):
            self.app.button(photo_actions,text,command).pack(side='left',padx=(0,8))
        photo_frame=ttk.Frame(p);photo_frame.grid(row=8,column=0,columnspan=3,sticky='ew');photo_frame.columnconfigure(0,weight=1);photo_frame.rowconfigure(0,weight=1)
        self.photos=ttk.Treeview(photo_frame,columns=('file','error'),show='headings',height=6);self.photos.heading('file',text='사진');self.photos.heading('error',text='오차 px');self.photos.column('file',width=580);self.photos.column('error',width=110,minwidth=110,stretch=False);self.photos.grid(row=0,column=0,sticky='nsew');self.photo_paths=[]
        self.photos.bind('<Double-1>',lambda e:self.app.guard(self.preview_calibration_photo))
        self.photo_scroll_y=AutoScrollbar(photo_frame,orient='vertical',command=self.photos.yview);self.photo_scroll_y.grid(row=0,column=1,sticky='ns')
        self.photo_scroll_x=AutoScrollbar(photo_frame,orient='horizontal',command=self.photos.xview);self.photo_scroll_x.grid(row=1,column=0,sticky='ew')
        self.photos.configure(yscrollcommand=self.photo_scroll_y.set,xscrollcommand=self.photo_scroll_x.set);self.bind_list_wheel(self.photos)
        photo_edit=ttk.Frame(p,style='Card.TFrame');photo_edit.grid(row=9,column=0,columnspan=3,sticky='w',pady=6)
        self.app.button(photo_edit,'선택 사진 제외',self.remove_photo).pack(side='left',padx=(0,8))
        self.app.button(photo_edit,'목록 초기화',lambda:(self.photo_paths.clear(),self.show_photos())).pack(side='left')
        self.field(p,10,'렌즈 보정 JSON','intrinsics_file','');self.button(p,10,'파일 선택',lambda:self.choose('intrinsics_file'));self.button(p,11,'렌즈 보정 불러오기',self.import_intrinsics)
        self.camera_info=tk.StringVar(value='기존 렌즈·카메라 설치 보정 사용');ttk.Label(p,textvariable=self.camera_info,wraplength=850).grid(row=12,column=0,columnspan=3,sticky='w',pady=8)
        self.label(p,13,'설치 위치를 바꿨을 때만: 체커 0번 코너 X·Y는 로봇 기준, Z는 바닥에서의 높이로 입력합니다.')
        for i,axis in enumerate('XYZ'):self.field(p,14+i,'체커 높이 Z mm (바닥=0)' if axis=='Z' else '로봇 기준 0번 코너 '+axis+' mm','origin_'+axis,0)
        self.field(p,17,'코너 열 방향 °','board_yaw',0);self.field(p,18,'코너 행 방향','row_sign','-Y',['-Y','+Y']);self.button(p,19,'현재 체커로 로봇 좌표 연결',self.save_extrinsics,0);self.button(p,19,'좌측 마운트 CAD값 복원',self.restore_extrinsics,1);self.button(p,19,'체커 원점 보기',self.inspect_board)
        self.restore_calibration_photos()
    def camera_source(self):
        value=self.value('cam_source')
        return self.camera_choices.get(value,value)
    def set_camera_choices(self,details):
        source=self.camera_source();prefix='Pi' if self.app.remote_mode else 'PC'
        self.camera_choices={f"{prefix} · {item['name']} · 컬러 ({item['device']})":item['source'] for item in details if item.get('kind')=='color'}
        label=next((label for label,path in self.camera_choices.items() if path==source),source)
        self.camera_source_choice.configure(values=list(self.camera_choices))
        self.vars['cam_source'].set(label)
    def refresh_camera_devices(self):
        if self.camera_devices_job:return
        remote=self.app.remote if self.app.remote_mode else None
        if self.app.remote_mode and not remote:raise ValueError('Pi에 연결한 뒤 카메라 목록을 새로고침하세요.')
        from .camera_inventory import camera_inventory
        def scan():
            if remote:return remote.http('/health',{},timeout=3.)['devices'].get('camera_details',[])
            return camera_inventory()
        self.camera_devices_job=(self.pool.submit(scan),self.app.remote_mode,remote)
        self.camera_refresh_btn.configure(text='확인 중…',state='disabled')
    def poll_camera_devices(self):
        if not self.camera_devices_job or not self.camera_devices_job[0].done():return
        job,mode,remote=self.camera_devices_job;self.camera_devices_job=None
        self.camera_refresh_btn.configure(text='목록 새로고침',state='normal')
        if mode!=self.app.remote_mode or mode and remote is not self.app.remote:return
        try:
            details=job.result();self.set_camera_choices(details)
            self.app.notice(f"{'Pi' if mode else 'PC'} 컬러 채널 {len(details)}개 · 선택 후 장치 설정 적용을 누르세요.")
        except Exception as exc:self.app.notice('카메라 목록 확인 실패: '+str(exc),True)
    def board(self):return int(self.value('board_cols')),int(self.value('board_rows')),float(self.value('board_mm'))
    def save_camera_device(self):
        self.require_calculation_idle()
        a=self.app
        if a.pending_execution or a.pose_latch.frozen:raise ValueError('실행이 끝난 뒤 카메라를 변경하세요.')
        w,h=int(self.value('cam_width')),int(self.value('cam_height'))
        if not 160<=w<=8192 or not 120<=h<=8192:raise ValueError('카메라 해상도 범위를 확인하세요.')
        a.profile['camera']={'source':self.camera_source(),'width':w,'height':h};a.persist_configuration();a.suspend_camera();a.camera_restart_pending=a.camera_needed();a.invalidate_jig_measurements();a.notice('카메라 설정 적용 · 필요할 때 새 설정으로 연결합니다.')
    def load_photos(self):
        folder=Path(self.value('photo_dir')).expanduser();self.photo_paths=sorted(p for p in folder.iterdir() if p.suffix.lower() in ('.jpg','.jpeg','.png','.bmp'));self.show_photos({})
    def show_photos(self,errors=None):
        if errors is not None:self.photo_errors=errors
        errors=getattr(self,'photo_errors',{})
        self.photos.delete(*self.photos.get_children())
        for i,p in enumerate(self.photo_paths):self.photos.insert('','end',iid=str(i),values=(p.name+(' · 파일 없음' if not p.is_file() else ''),errors.get(str(p),'—')))
    def restore_calibration_photos(self):
        data=self.app.profile.get('intrinsics') or {};files=data.get('source_files') or []
        folder=Path(data.get('source_directory') or self.app.data_dir/'camera_images').expanduser()
        if not folder.is_absolute():folder=self.app.data_dir/folder
        self.photo_paths=[Path(name).expanduser() if Path(name).expanduser().is_absolute() else folder/name for name in files]
        if files and not data.get('source_directory'):folder=self.photo_paths[0].parent
        self.vars['photo_dir'].set(str(folder))
        board=data.get('board') or {}
        for key,source in (('board_cols','cols'),('board_rows','rows'),('board_mm','square_mm')):
            if source in board:self.vars[key].set(str(board[source]))
        errors={str(p):(f'{error:.3f}' if isinstance(error,(int,float)) else '—') for p,error in zip(self.photo_paths,data.get('errors_px') or [])}
        self.show_photos(errors)
        from .camera_calibration import validate_intrinsics
        try:validate_intrinsics(data)
        except (KeyError,ValueError,TypeError):self.camera_info.set('유효한 렌즈 보정값 없음');return
        found=sum(p.is_file() for p in self.photo_paths)
        text='렌즈 보정 적용됨 · '+(f'사용 사진 {len(files)}장 · 파일 확인 {found}장' if files else '사용 사진 기록 없음')
        if isinstance(data.get('rms_px'),(int,float)):text+=f" · RMS {data['rms_px']:.3f}px"
        source=(self.app.profile.get('extrinsics') or {}).get('source')
        if source=='cad_estimate':text+='\n로봇 좌표 연결: CAD 추정값'
        elif source=='shared_camera_and_preview_base_transform':text+='\n로봇 좌표 연결: 공유 카메라 좌표 변환값'
        elif source=='measured_board':text+='\n로봇 좌표 연결: 체커보드 측정값'
        self.camera_info.set(text+'\n사진을 더블클릭하면 보정에 사용한 원본을 확인할 수 있습니다.')
    def preview_calibration_photo(self):
        chosen=self.photos.selection()
        if not chosen:return
        path=self.photo_paths[int(chosen[0])]
        if not path.is_file():raise ValueError('보정 사진 파일이 없습니다: '+str(path))
        from PIL import Image,ImageTk
        with Image.open(path) as source:image=source.convert('RGB')
        image.thumbnail((850,460));page=ttk.Frame(self.tabs,padding=12);previous=self.tabs.select();self.tabs.add(page,text='보정 사진');self.tabs.select(page)
        ttk.Label(page,text=str(path),wraplength=850).pack(anchor='w',pady=8)
        page.photo=ImageTk.PhotoImage(image,master=page);ttk.Label(page,image=page.photo).pack()
        def close():self.tabs.select(previous);self.tabs.forget(page);page.destroy()
        self.app.button(page,'보정 목록으로 돌아가기',close).pack(anchor='e',pady=8)
    def remove_photo(self):
        ids=set(int(i) for i in self.photos.selection());self.photo_paths=[p for i,p in enumerate(self.photo_paths) if i not in ids];self.show_photos()
    def frame(self):
        a=self.app
        if not a.camera or not a.camera.observation or time.monotonic()-a.camera.observation[2]>1:raise ValueError('최신 카메라 영상이 없습니다.')
        return a.camera.observation[0].copy()
    def capture_board(self):
        self.app.request_camera_photo(self.save_board_frame)
    def save_board_frame(self,frame):
        import cv2
        from .camera_calibration import corners
        cols,rows,_=self.board();corners(frame,cols,rows);p=self.app.data_dir/'camera_images'/f'board-{time.time_ns()}.png';p.parent.mkdir(parents=True,exist_ok=True)
        if not cv2.imwrite(str(p),frame):raise OSError('사진 저장 실패')
        self.photo_paths.append(p);self.show_photos()
    def run_job(self,fn,done):
        if self.job:raise ValueError('계산이 진행 중입니다.')
        self.job=(self.pool.submit(fn),done);self.app.notice('보정 계산 중 · 창을 이동해도 계속 계산합니다.')
    def calibrate_camera(self):
        self.require_calculation_idle()
        from .camera_calibration import calibrate_images
        args=self.board();paths=list(self.photo_paths);profile=deepcopy(self.app.profile)
        def done(result):
            self.apply_camera_geometry('intrinsics',result,expected_profile=profile);self.restore_calibration_photos()
        self.run_job(lambda:calibrate_images(paths,*args),done)
    def import_intrinsics(self):
        self.require_calculation_idle()
        from .camera_calibration import validate_intrinsics
        d=read_json(self.value('intrinsics_file'));d=d.get('intrinsics',d);validate_intrinsics(d);self.apply_camera_geometry('intrinsics',d);self.restore_calibration_photos()
    def save_extrinsics(self):
        self.require_calculation_idle();self.app.request_camera_photo(self.save_extrinsics_frame)
    def save_extrinsics_frame(self,f):
        from .camera_calibration import board_extrinsics
        args=self.board();p=deepcopy(self.app.profile);origin=[float(self.value('origin_'+axis) or '0') for axis in 'XYZ'];origin[2]=base_z_from_height(origin[2],p);yaw=float(self.value('board_yaw'));sign=-1 if self.value('row_sign')=='-Y' else 1
        def done(d):self.apply_camera_geometry('extrinsics',d,expected_profile=p);self.camera_info.set('체커보드 기준 로봇 좌표 연결 적용')
        self.run_job(lambda:board_extrinsics(f,p['intrinsics'],origin,yaw,*args,row_sign=sign),done)
    def inspect_board(self):
        self.app.request_camera_photo(self.inspect_board_frame)
    def inspect_board_frame(self,frame):
        import cv2,numpy as np
        from PIL import Image,ImageTk
        from .camera_calibration import corners
        cols,rows,_=self.board();points=corners(frame,cols,rows).reshape(-1,2);p0=tuple(np.rint(points[0]).astype(int))
        cv2.drawChessboardCorners(frame,(cols,rows),points.reshape(-1,1,2),True)
        cv2.putText(frame,'0',p0,cv2.FONT_HERSHEY_SIMPLEX,1,(0,0,255),3)
        for index,text,color in [(cols-1,'+COL',(0,0,255)),((rows-1)*cols,'+ROW',(0,180,0))]:
            end=tuple(np.rint(points[index]).astype(int));cv2.arrowedLine(frame,p0,end,color,3);cv2.putText(frame,text,end,cv2.FONT_HERSHEY_SIMPLEX,.8,color,2)
        page=ttk.Frame(self.tabs,padding=12);previous=self.tabs.select();self.tabs.add(page,text='체커 원점 확인');self.tabs.select(page)
        ttk.Label(page,text='현재 영상의 확인 사진입니다. 빨강 0번 코너·열 방향과 초록 행 방향을 입력값에 대응하세요.').pack(anchor='w',pady=8)
        image=Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB));image.thumbnail((850,460));page.photo=ImageTk.PhotoImage(image,master=page);ttk.Label(page,image=page.photo).pack()
        def close():self.tabs.select(previous);self.tabs.forget(page);page.destroy()
        self.app.button(page,'설정으로 돌아가기',close).pack(anchor='e',pady=8)
    def apply_camera_geometry(self,key,value,*,expected_profile=None):
        self.require_calculation_idle()
        if key not in ('intrinsics','extrinsics'):raise ValueError('카메라 계산 기준 항목 오류')
        if expected_profile is not None and self.app.profile!=expected_profile:
            raise ValueError('계산 중 로봇·카메라 설정이 바뀌었습니다. 현재 설정으로 보정을 다시 계산하세요.')
        value=deepcopy(value)
        if self.app.remote_mode and self.app.remote:
            if self.app.remote.error:raise ValueError('Pi 통신을 복구한 뒤 카메라 보정을 적용하세요.')
            from .remote_config import configuration_bundle
            bundle=configuration_bundle(self.app);bundle['profile'][key]=value
            self.app.remote.sync_bundle(bundle)
        self.app.profile[key]=value;self.app.persist_configuration();self.app.invalidate_jig_measurements()
    def restore_extrinsics(self):
        from .arm_workspace import camera_reference_for_profile
        value=camera_reference_for_profile(self.app.profile,read_json(ROOT/'assets/reference/camera.json')['extrinsics'])
        self.apply_camera_geometry('extrinsics',value);self.camera_info.set('선택 팔 좌표의 좌측 마운트 CAD 추정값 적용 · 실측 보정 아님')
    def build_tcp(self):
        p=self.form('tcp');d=self.app.profile['tcp'];self.label(p,0,'오른쪽 모델의 A(파랑)에서 B(주황)까지의 차이입니다. B는 선택한 TCP입니다. 끝단은 현재 사용하던 STL 끝점, 중앙은 기존 모델 원점(XYZ 0, 0, 0)입니다.')
        selector=self.field(p,1,'TCP 기준','tcp_mode',tcp_label(d),[*TCP_PRESETS,'직접 설정'])
        self.tcp_fields=[]
        for i,axis in enumerate('XYZ'):self.tcp_fields.append(self.field(p,2+i,'집게 로컬 '+axis+' mm','tcp_xyz_'+axis,f"{d['xyz_mm'][i]:.6f}"))
        for i,axis in enumerate(('roll','pitch','yaw')):self.tcp_fields.append(self.field(p,5+i,axis+' °','tcp_rpy_'+axis,f"{d['rpy_deg'][i]:.6f}"))
        selector.bind('<<ComboboxSelected>>',lambda e:self.update_tcp_inputs());self.update_tcp_inputs()
        self.button(p,8,'끝단 값 불러오기',self.load_model_tcp,0);self.button(p,8,'TCP 적용·저장',self.save_tcp,1)
        self.label(p,9,'미리보기는 입력한 값을 보여줍니다. 적용·저장을 눌러야 실행 계산에 반영됩니다. 모터 영점은 바꾸지 않습니다.')
        self.label(p,10,'A는 집게 끝 근처에 정의된 모델 원점입니다. 모델 내부 이름: gripper_frame_link. 베이스 원점이나 손목 모터 중심이 아닙니다.')
    def selected_tcp(self):
        choice=self.value('tcp_mode')
        if choice in TCP_PRESETS:return model_tcp(point=TCP_PRESETS[choice])
        if choice!='직접 설정':raise ValueError('TCP 기준을 선택하세요.')
        return {'mode':'manual','xyz_mm':[float(self.value('tcp_xyz_'+k)) for k in 'XYZ'],
                'rpy_deg':[float(self.value('tcp_rpy_'+k)) for k in ('roll','pitch','yaw')],'verified':False}
    def update_tcp_inputs(self):
        automatic=self.value('tcp_mode') in TCP_PRESETS
        if automatic:
            d=self.selected_tcp()
            for i,axis in enumerate('XYZ'):self.vars['tcp_xyz_'+axis].set(f"{d['xyz_mm'][i]:.6f}")
            for axis in ('roll','pitch','yaw'):self.vars['tcp_rpy_'+axis].set('0.000000')
        for field in self.tcp_fields:field.configure(state='readonly' if automatic else 'normal')
    def load_model_tcp(self):
        self.vars['tcp_mode'].set('고정 집게 끝단');self.update_tcp_inputs()
    def save_tcp(self):
        self.require_calculation_idle()
        if self.app.playing or self.app.pending_execution or self.app.session and self.app.session.running and self.app.session.state!='READ_ONLY':raise ValueError('실행과 토크를 해제한 뒤 TCP를 변경하세요.')
        d=self.selected_tcp()
        tcp_matrix(d)
        if self.app.remote_mode and self.app.remote and not self.app.remote.error:
            from .remote_config import configuration_bundle
            bundle=configuration_bundle(self.app);bundle['profile']['tcp']=deepcopy(d)
            self.app.remote.sync_bundle(bundle)
        self.app.profile['tcp']=d;self.app.persist_configuration();self.app.update_kinematics();self.app.notice('TCP 저장 · '+tcp_label(d)+' · 모터 영점은 유지됩니다.')
    def build_help(self):
        from .help_content import HELP_SECTIONS
        p=self.form('help');self.label(p,0,'빠른 시작 · 할 작업을 선택하세요. F1로 언제든 이 안내를 다시 열 수 있습니다.')
        quick=ttk.Frame(p,style='Card.TFrame');quick.grid(row=1,column=0,columnspan=3,sticky='w',pady=(4,12))
        quick.columnconfigure((0,1),weight=1,minsize=430)
        def teaching():
            self.app.show_page('teach');self.app.teach_tabs.select(self.app.teach_overview)
        def inspection():
            self.app.open_episode_adjust();self.app.episode_adjust_panel.tabs.select(self.app.episode_adjust_panel.inspection)
        actions=(('1. 연결 설정',lambda:self.tabs.select(self.pages['pi']),'Pi 주소와 연결 상태 확인'),('2. 스텝 만들기',teaching,'자세 편집 → 새 스텝 추가 → 경로 미리보기'),('3. 완제품·안착 검사',inspection,'완제품 선택 → 검사할 스텝 선택 → 검사 저장'),('4. Pi로 내보내기',lambda:self.episode_transfer_panel.open(),'에피소드 조정 → 저장 상태 조회 → 전송'))
        for index,(title,action,hint) in enumerate(actions):
            cell=ttk.Frame(quick,style='Card.TFrame');cell.grid(row=index//2,column=index%2,sticky='ew',padx=(0,12),pady=5)
            self.app.button(cell,title,action).pack(anchor='w')
            ttk.Label(cell,text=hint,style='Small.TLabel',wraplength=380).pack(anchor='w',pady=3)
        self.label(p,2,'동작 정지 · 자세 유지: 움직임을 멈추고 힘 유지  |  토크 해제 · Esc: 모터 힘 끄기\n입력칸 편집과 3D 미리보기는 실물 실행과 별개입니다. 아래에서 자세한 사용법을 확인하세요.')
        self.help_image=None
        for i,(title,text) in enumerate(HELP_SECTIONS):
            ttk.Label(p,text=title,style='Section.TLabel').grid(row=3+i*2,column=0,columnspan=3,sticky='w',pady=(14,4))
            self.label(p,4+i*2,text)
    def apply_completed_calibration(self):
        role=self.calibration_role;a=self.app;active_port=a.profile['port'] if role=='follower' else a.profile.get('leader',{}).get('port')
        if role=='follower' and self.worker.port!=active_port:return
        profile=deepcopy(a.profile);path,cal=self.library.copy_calibration(self.worker.destination,role)
        if role=='follower':
            profile=self.library.reference_for(profile,cal);profile['calibration_file']=path
            profile['calibration_status']='ready' if cal.angle_mapping else 'pending'
        else:profile['leader']={'port':self.worker.port,'calibration_file':path,'angle_mapping_sha256':cal.angle_mapping.sha256 if cal.angle_mapping else None}
        a.install_profile(profile);self.active_profile_key=self.library.save(profile['name'],profile,self.active_profile_key);self.refresh_profiles();self.fill_robot();a.notice(('3점 각도 보정·모터 영점 적용' if cal.angle_mapping else '모터 영점 적용 · 3점 자료 없음')+' · 기존 에피소드는 보존합니다.')
    def open_diagnostics(self):
        self.app.show_page('settings')
        if getattr(self,'diagnostic_page',None) is not None:
            self.tabs.select(self.diagnostic_page);self.refresh_diagnostics();return
        page=ttk.Frame(self.tabs,padding=12);self.diagnostic_page=page;self.tabs.add(page,text='진단 기록');self.tabs.select(page)
        page.rowconfigure(0,weight=1);page.columnconfigure(1,weight=1)
        paths=sorted((self.app.data_dir/'diagnostics').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        files=ttk.Frame(page);files.grid(row=0,column=0,sticky='nsew',padx=(0,10));files.rowconfigure(0,weight=1);files.columnconfigure(0,weight=1)
        listing=tk.Listbox(files,width=34,exportselection=False);listing.grid(row=0,column=0,sticky='nsew');self.bind_list_wheel(listing)
        file_y=AutoScrollbar(files,orient='vertical',command=listing.yview);file_y.grid(row=0,column=1,sticky='ns')
        file_x=AutoScrollbar(files,orient='horizontal',command=listing.xview);file_x.grid(row=1,column=0,sticky='ew')
        listing.configure(yscrollcommand=file_y.set,xscrollcommand=file_x.set)
        content=ttk.Frame(page);content.grid(row=0,column=1,sticky='nsew');content.rowconfigure(0,weight=1);content.columnconfigure(0,weight=1)
        text=tk.Text(content,wrap='none',font=('Noto Sans CJK KR',11),width=40);text.grid(row=0,column=0,sticky='nsew');self.bind_list_wheel(text)
        text_y=AutoScrollbar(content,orient='vertical',command=text.yview);text_y.grid(row=0,column=1,sticky='ns')
        text_x=AutoScrollbar(content,orient='horizontal',command=text.xview);text_x.grid(row=1,column=0,sticky='ew')
        text.configure(yscrollcommand=text_y.set,xscrollcommand=text_x.set)
        def selected(e=None):
            if not listing.curselection():return
            data=read_json(paths[listing.curselection()[0]]);text.configure(state='normal');text.delete('1.0','end');text.insert('1.0',json.dumps(data,ensure_ascii=False,indent=2));text.configure(state='disabled');text.yview_moveto(0);text.xview_moveto(0)
        listing.bind('<<ListboxSelect>>',lambda e:self.app.guard(selected))
        def refresh():
            nonlocal paths
            paths=sorted((self.app.data_dir/'diagnostics').glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            listing.delete(0,'end')
            for path in paths:listing.insert('end',path.name)
            if paths:listing.selection_set(0);selected()
            else:
                text.configure(state='normal');text.delete('1.0','end');text.insert('1.0','저장된 진단 기록이 없습니다.');text.configure(state='disabled')
        self.refresh_diagnostics=refresh;refresh()
    def poll(self):
        import queue
        if hasattr(self.app,'episode_sync'):self.app.episode_sync.poll()
        self.pi_panel.poll()
        self.episode_transfer_panel.poll()
        self.poll_camera_devices()
        if self.worker:
            while True:
                try:kind,value=self.worker.events.get_nowait()
                except queue.Empty:break
                if kind=='positions':
                    self.calibration_panel.update(value)
                elif kind=='state':
                    if value=='DONE':
                        if self.app.remote_mode and self.worker.running:self.pending_calibration_apply=True
                        else:self.apply_completed_calibration()
                    self.calibration_panel.state_changed(value)
                elif kind=='saved':self.vars[self.calibration_role+'_json'].set(value);self.app.notice('보정 JSON과 모터 영점 저장 완료')
                else:self.cal_status.set(str(value));self.app.notice(str(value),kind=='error')
        if getattr(self,'pending_calibration_apply',False) and self.worker and not self.worker.running:
            self.pending_calibration_apply=False;self.apply_completed_calibration()
        if self.job and self.job[0].done():
            if self.calculation_busy():
                self.camera_info.set('보정 계산 완료 · 현재 측정·실행이 끝나면 적용합니다.');return
            job,done=self.job;self.job=None
            try:done(job.result())
            except Exception as exc:self.app.notice(str(exc),True);self.camera_info.set(str(exc))
    def mark_model_dirty(self,key):
        self.model_dirty[key]=time.monotonic()+.2
        if key in self.model_views:self.model_views[key].request=None
    def model_spec(self,key):
        if key=='tcp':
            d=self.selected_tcp()
            tcp_matrix(d);return {'kind':'tcp','tcp':d}
        path=self.value('jig_stl');path=Path(path).expanduser().resolve() if path else None
        if path and not path.is_file():raise ValueError('선택한 STL 파일을 찾을 수 없습니다.')
        spec={'kind':'jig','stl':str(path) if path else None,'mtime':path.stat().st_mtime_ns if path else None,'unit':self.value('jig_unit'),
              'size_mm':[float(self.value('jig_width')),float(self.value('jig_depth'))] if not path else None,'rim_mm':float(self.value('jig_rim')) if not path else None}
        registered=self.app.catalog.items.get(self.jig_id,{})
        if path and registered.get('stl') and path==Path(registered['stl']).resolve() and spec['unit']==registered['unit']:
            mesh=self.app.catalog.mesh(self.jig_id);installation=registered.get('physical_installation',{})
            if mesh.get('assembly',{}).get('orientation_hole'):
                spec['preview_jig']={'stl':str(path),'unit':spec['unit'],'low_mm':mesh['low_mm'],'size_mm':mesh['size_mm'],
                    'mesh_yaw_offset_deg':180.,'platform':installation.get('platform'),
                    'platform_height_mm':installation.get('carrier_underside_height_mm'),
                    'platform_yaw_deg':installation.get('platform_yaw_deg',-90.)}
        return spec
    def poll_models(self):
        if self.models_closed:return
        try:
            active=next((k for k in self.model_views if str(self.pages[k])==self.tabs.select()),None) if self.app.workspace_visible and self.app.page=='settings' else None
            if active:
                pane=self.model_views[active];due=self.model_dirty.get(active)
                # A chosen file may be regenerated without an entry-field edit.
                # Refresh its preview only; registering it still requires Save.
                if active=='jigs' and due is None and time.monotonic()>=self.model_file_check_at.get(active,0.):
                    self.model_file_check_at[active]=time.monotonic()+.5;due=0.
                if due is not None and time.monotonic()>=due:
                    self.model_dirty.pop(active,None)
                    try:
                        spec=self.model_spec(active);pane.set_spec(spec)
                        if active=='jigs':
                            from .vision import stl_profile
                            self.update_jig_method_names(stl_profile(spec['stl'],unit=spec['unit']) if spec.get('stl') else None)
                    except Exception as exc:pane.set_spec(None,str(exc))
                if pane.spec is not None and active not in self.model_dirty and self.app.render_enabled:
                    request=(pane.version,pane.view)
                    if pane.request!=request:
                        from .preview import Renderer
                        from .inspection import inspection_worker
                        if self.model_renderer is None:self.model_renderer=Renderer(worker_target=inspection_worker)
                        self.model_renderer.submit((),pane.view,pane.spec,context=(active,pane.version));pane.request=request
            if self.model_renderer:
                item=self.model_renderer.poll()
                if item and item[0]=='error':
                    if active:self.model_views[active].caption.set('모델 표시 실패: '+item[1])
                    self.model_renderer.close();self.model_renderer=None
                elif item:
                    key,version=item[3];pane=self.model_views.get(key)
                    if pane and pane.version==version:pane.accept(item)
        except Exception as exc:
            if active:self.model_views[active].set_spec(None,'모델 표시 오류: '+str(exc))
        finally:self.model_job=self.app.root.after(33,self.poll_models)
    def close(self):
        self.calibration_panel.close()
        self.models_closed=True
        self.app.root.after_cancel(self.model_job)
        if self.model_renderer:self.model_renderer.close()
        if self.worker:self.worker.close()
        self.pool.shutdown(wait=False,cancel_futures=True)
