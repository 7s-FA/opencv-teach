"""Hand-guided three-point calibration, with an always-visible model beside the form."""
import io,math,time
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image
from .domain import JOINTS,LABELS,Calibration
from .preview import Renderer,pan_view,DEFAULT_LOOKAT
from .angle_mapping import points_ready

STATES={'IDLE':'몸체: 0°·양쪽 각도 / 집게: −90°·닫힘·열림 → 범위 → 저장',
 'MIDPOINT':'관절 선택 → 몸체 0° 또는 집게 −90° 기준을 기록하세요.',
 'ANGLES':'몸체는 0°·양쪽 각도, 집게는 −90°·닫힘·열림을 기록하세요.',
 'RANGE':'몸체 5개 관절의 접촉 한계를 측정하세요. 집게는 기록한 닫힘·열림을 유지합니다.',
 'VERIFY':'4. 기록한 각도·틱·최소~최대 범위를 확인하고 최종 저장하세요.',
 'DONE':'저장 완료 · 모터 전원을 껐다 켠 뒤 다시 연결하세요.',
 'CLOSED':'보정 중단 · 진행 기록 유지 · 이전 서보 설정 복원', 'FAULT':'보정 정리 실패 · 오류와 복원 기록을 확인하세요.'}

class CalibrationPanel:
    def __init__(self,settings):
        self.s=settings;self.a=settings.app;self.renderer=None;self.request=None;self.image=None;self.closed=False;self.last_sample=None;self.view=(100.,-18.,.82);self.view_owner=None;self.other_safe_angles=None;self.drag=None;self.preview_kind='guide';self.guide=0.;self.guide_source='zero';self.selected_joint=None;self.angle_drafts={};self.state='IDLE'
        outer=settings.pages['calibration'];p=settings.form('calibration');p.inspection_form=True
        outer.columnconfigure(0,weight=3);outer.columnconfigure(2,weight=3,minsize=350)
        right=ttk.Frame(outer,padding=14,style='Card.TFrame');right.grid(row=0,column=2,sticky='nsew',padx=(8,0));right.columnconfigure(0,weight=1);right.rowconfigure(2,weight=1)
        ttk.Label(right,text='서보 각도 · 3D 기준 자세',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.caption=tk.StringVar(value='청록색: 선택 관절 · 버튼은 모델만 움직입니다.')
        summary=ttk.Frame(right,style='Card.TFrame');summary.grid(row=1,column=0,sticky='ew',pady=8);summary.columnconfigure(1,weight=1)
        angle=ttk.Frame(summary,style='Card.TFrame');angle.grid(row=0,column=0,sticky='nw',padx=(0,16))
        self.angle_kind=tk.StringVar(value='기준 각도');self.angle_value=tk.StringVar(value='—')
        ttk.Label(angle,textvariable=self.angle_kind,style='Small.TLabel').pack(anchor='w')
        self.angle_label=ttk.Label(angle,textvariable=self.angle_value,font=('Noto Sans CJK KR',30,'bold'),foreground='#11697a')
        self.angle_label.pack(anchor='w')
        context=ttk.Label(summary,textvariable=self.caption,wraplength=270);context.grid(row=0,column=1,sticky='w')
        summary.bind('<Configure>',lambda e:context.configure(wraplength=max(120,e.width-angle.winfo_reqwidth()-16)))
        self.canvas=tk.Canvas(right,bg='#dfe7ed',highlightthickness=0,width=350,height=300);self.canvas.grid(row=2,column=0,sticky='nsew')
        self.canvas.bind('<Configure>',lambda e:self.a.fit_image(self.canvas,self.image) if self.image else None)
        bar=ttk.Frame(right);bar.grid(row=3,column=0,sticky='ew',pady=8)
        self.guide_buttons={}
        for label,kind in [('음수 각도','negative'),('0°','zero'),('양수 각도','positive'),('읽은 자세','live')]:
            button=self.a.button(bar,label,lambda k=kind:self.show(k));button.pack(side='left',expand=True,fill='x',padx=2);self.guide_buttons[kind]=button
        self.detail=tk.StringVar(value='영점 자세에서 서보 출력축이 회전한 각도입니다.\n접촉 한계와 각도 기준점은 따로 기록합니다.')
        ttk.Label(right,textvariable=self.detail,wraplength=430,style='Muted.TLabel').grid(row=4,column=0,sticky='w',pady=6)
        self.a.button(right,'시점 복원',self.reset_view).grid(row=5,column=0,sticky='e')
        self.canvas.bind('<ButtonPress-1>',lambda e:setattr(self,'drag',(e.x,e.y,self.view)))
        self.canvas.bind('<B1-Motion>',self.rotate)
        self.canvas.bind('<ButtonPress-3>',lambda e:setattr(self,'drag',(e.x,e.y,self.view)))
        self.canvas.bind('<B3-Motion>',self.pan)
        self.canvas.bind('<Button-4>',lambda e:self.zoom(.9));self.canvas.bind('<Button-5>',lambda e:self.zoom(1.1))
        settings.label(p,0,'토크 OFF에서 손으로 맞춥니다. 몸체는 0°, 집게는 −90° 자세를 2047틱으로 기록합니다. 선택한 관절만 변경합니다.')
        self.role=settings.field(p,1,'보정할 팔','cal_role','리더' if (self.a.data_dir/'calibration_progress/leader.json').exists() else '팔로워',['팔로워','리더'])
        self.start=settings.button(p,1,'보정 시작',settings.start_calibration)
        self.port=settings.field(p,2,'보정할 포트','cal_port',settings.value('follower_port'),list(map(str,Path('/dev/serial/by-id').glob('*'))));self.port.configure(state='normal')
        self.port_name=tk.StringVar();ttk.Label(p,textvariable=self.port_name,style='Small.TLabel').grid(row=2,column=2,sticky='w',padx=8)
        settings.vars['cal_port'].trace_add('write',lambda *args:self.update_port_name());self.update_port_name()
        self.role.bind('<<ComboboxSelected>>',lambda e:self.sync_role())
        settings.field(p,3,'기존 영점 JSON','cal_import','');self.choose=settings.button(p,3,'파일 선택',lambda:settings.choose('cal_import'))
        file_actions=ttk.Frame(p,style='Card.TFrame');file_actions.grid(row=4,column=0,columnspan=3,sticky='ew')
        file_actions.columnconfigure(0,weight=1);file_actions.columnconfigure(1,weight=1)
        self.inspect_btn=settings.button(file_actions,0,'저장 내용 확인',self.preview_saved,0)
        self.import_btn=settings.button(file_actions,0,'모터 적용',lambda:settings.start_calibration(True),1)
        settings.label(p,5,'각도·집게 범위 파일(.angles.json)은 영점 JSON 옆에서 함께 불러옵니다.')
        settings.cal_status=tk.StringVar(value=STATES['IDLE']);ttk.Label(p,textvariable=settings.cal_status,wraplength=430).grid(row=6,column=0,columnspan=3,sticky='w',pady=6)
        self.progress=tk.StringVar(value='영점 0/6 · 3점 완료 0/6');ttk.Label(p,textvariable=self.progress,style='Small.TLabel').grid(row=7,column=0,sticky='w')
        self.cancel=settings.button(p,7,'중단·진행 기록 유지',lambda:settings.worker.close() if settings.worker else None,1);self.cancel.grid_configure(columnspan=2)
        cols=('joint','tick','negative','zero','positive','range');settings.cal_table=ttk.Treeview(p,columns=cols,show='headings',height=6,style='Step.Treeview',selectmode='browse')
        for k,title,w in zip(cols,('관절','현재 틱','음수°/틱','기준 틱','양수°/틱','최소~최대'),(80,48,74,48,74,85)):
            settings.cal_table.heading(k,text=title);settings.cal_table.column(k,width=w,minwidth=40,stretch=True)
        for n,l in zip(JOINTS,LABELS):settings.cal_table.insert('','end',iid=n,values=(l,'—','—','—','—','—'))
        settings.cal_table.grid(row=8,column=0,columnspan=3,sticky='ew',pady=6);settings.cal_table.selection_set(JOINTS[0]);settings.cal_table.bind('<<TreeviewSelect>>',lambda e:self.select())
        self.joint=settings.field(p,9,'기록할 관절','cal_joint',LABELS[0],list(LABELS));self.joint.bind('<<ComboboxSelected>>',lambda e:self.select_combo())
        self.zero=settings.button(p,9,'선택 관절 0° 기록',lambda:settings.cal_command(('zero',self.name())))
        self.negative_input=settings.field(p,10,'음수 기준각 °','cal_negative',-90);self.negative=settings.button(p,10,'현재 틱 기록',lambda:self.record('negative'))
        self.positive_input=settings.field(p,11,'양수 기준각 °','cal_positive',90);self.positive=settings.button(p,11,'현재 틱 기록',lambda:self.record('positive'))
        self.negative_label=p.grid_slaves(row=10,column=0)[0];self.positive_label=p.grid_slaves(row=11,column=0)[0]
        self.rule=tk.StringVar();ttk.Label(p,textvariable=self.rule,wraplength=430,style='Small.TLabel').grid(row=12,column=0,columnspan=3,sticky='w',pady=8)
        actions=ttk.Frame(p,style='Card.TFrame');actions.grid(row=13,column=0,columnspan=3,sticky='ew',pady=(4,0))
        actions.columnconfigure((0,1),weight=1,uniform='calibration_actions')
        self.range=settings.button(actions,0,'몸체 범위 측정',lambda:settings.cal_command('range'),0)
        self.review=settings.button(actions,0,'측정 결과 확인',lambda:settings.cal_command('review'),1)
        self.back=settings.button(actions,1,'기준 다시 기록',lambda:settings.cal_command('angles'),0)
        self.save=settings.button(actions,1,'최종 저장·적용',lambda:settings.cal_command('save'),1)
        for button in (self.range,self.review,self.back,self.save):button.grid_configure(sticky='ew',padx=(0,8),pady=4)
        self.state_changed('IDLE');self.sync_role();self.job=self.a.root.after(34,self.poll)
    def update_port_name(self):
        value=self.s.value('cal_port')
        try:
            name=Path(value).name.split('_')[-1].split('-if')[0] if value else ''
            self.port_name.set('USB …'+name[-8:] if name else '포트 미선택')
        except (OSError,RuntimeError):self.port_name.set('경로 확인 필요')
    def sync_role(self):
        role='leader' if self.s.value('cal_role')=='리더' else 'follower'
        from .arm_workspace import arm_id
        owner=(role,arm_id(self.a.profile))
        self.load_other_safe_pose()
        if owner!=self.view_owner:self.reset_view();self.view_owner=owner
        ports=self.s.device_ports(role);port=self.s.value(role+'_port')
        if not port and role=='leader':
            others=[p for p in ports if self.a.remote_mode or Path(p).resolve()!=Path(self.s.value('follower_port')).resolve()]
            if len(others)==1:port=others[0]
        self.port.configure(values=ports);self.s.vars['cal_port'].set(port);self.s.vars['cal_import'].set(self.s.value(role+'_json'))
        self.last_sample=None;self.angle_drafts={}
        self.update({'current':dict.fromkeys(JOINTS,'—'),'mins':{},'maxs':{},'points':{},'verification':{}});self.load_degrees();self.show('zero')
        if self.a.remote_mode and role=='follower':
            self.start.configure(text='Pi 보정 시작·이어가기');self.s.cal_status.set('팔로워: Pi USB · 시작하면 Pi에 저장된 진행 기록을 불러옵니다.');return
        from .arm_workspace import progress_path
        progress=progress_path(self.a.data_dir,role,self.a.profile)
        self.start.configure(text='이어서 보정' if progress.exists() else '보정 시작')
        if progress.exists():
            from .calibration import validate_progress
            from .domain import read_json
            try:
                doc=read_json(progress);validate_progress(doc,port)
                self.update({'current':dict.fromkeys(JOINTS,'—'),**{k:doc.get(k,{}) for k in ('points','mins','maxs','verification')}});self.load_degrees()
                self.s.cal_status.set('저장된 진행 기록을 표시합니다. 이어서 보정을 눌러 계속하세요.');return
            except (ValueError,OSError,KeyError,TypeError) as exc:self.s.cal_status.set('진행 기록 확인 필요: '+str(exc));return
        self.s.cal_status.set(('리더' if role=='leader' else '팔로워')+' 전용 보정 · 포트를 확인하고 시작하세요.')
    def preview_saved(self):
        if self.s.worker and self.s.worker.running:raise ValueError('진행 중인 보정을 마친 뒤 파일을 확인하세요.')
        role='leader' if self.s.value('cal_role')=='리더' else 'follower'
        path=self.s.value('cal_import') or self.s.value(role+'_json')
        cal=Calibration(path);mapping=cal.angle_mapping
        self.update({'current':dict.fromkeys(JOINTS,'—'),'mins':{n:m.low for n,m in cal.motors.items()},'maxs':{n:m.high for n,m in cal.motors.items()},'points':mapping.document['joints'] if mapping else {},'verification':mapping.document.get('verification',{}) if mapping else {}})
        self.load_degrees();self.show('zero')
        self.s.cal_status.set('저장된 3점 보정 확인 · 모터 설정은 변경하지 않았습니다.' if mapping else '영점·범위만 있는 파일입니다. 3점 각도 자료는 없습니다.')
    def name(self):return JOINTS[list(LABELS).index(self.s.value('cal_joint'))]
    def select(self):
        chosen=self.s.cal_table.selection()
        if chosen:
            name=chosen[0]
            if name==self.selected_joint:return
            if self.selected_joint:self.angle_drafts[self.selected_joint]={k:self.s.value('cal_'+k) for k in ('negative','positive')}
            self.selected_joint=name;self.s.vars['cal_joint'].set(LABELS[JOINTS.index(name)]);self.load_degrees();self.update_record_controls();self.request=None
    def select_combo(self):self.s.cal_table.selection_set(self.name());self.select()
    def load_degrees(self):
        points=(self.last_sample or {}).get('points',{}).get(self.name(),{})
        if self.name()=='gripper':
            for key,side in [('negative','closed'),('positive','open')]:self.s.vars['cal_'+key].set(str(points.get(side,{}).get('ticks','—')))
            return
        for key,default in [('negative',-90),('positive',90)]:self.s.vars['cal_'+key].set(str(points.get(key,{}).get('degrees',self.angle_drafts.get(self.name(),{}).get(key,default))))
    def record(self,side):
        if self.name()=='gripper':
            self.s.cal_command(('grip_endpoint','closed' if side=='negative' else 'open'));self.show('live');return
        deg=float(self.s.value('cal_'+side));self.s.cal_command(('record',self.name(),side,deg));self.show(side)
    def show(self,kind):
        self.preview_kind='live' if kind=='live' else 'guide';self.guide_source=kind
        if kind!='live' and self.name()!='gripper':self.guide=0. if kind=='zero' else float(self.s.value('cal_'+kind))
        self.request=None
    def preview_placement(self):
        if self.s.value('cal_role')=='리더':return None
        if self.a.workspace_manager:return self.a.workspace_manager.live_workcell(self.a,safe=True)
        from .arm_workspace import scene_placement
        return scene_placement(self.a.workcell_preview,self.a.profile,self.other_safe_angles)
    def load_other_safe_pose(self):
        from .arm_workspace import arm_id,calibration_pending
        from .domain import EpisodeStore,ModelReference
        self.other_safe_angles=None
        other='arm2' if arm_id(self.a.profile)=='arm3' else 'arm3'
        profile=next((p for p in self.s.library.items.values() if arm_id(p)==other),None)
        if not profile or calibration_pending(profile):return
        cal=Calibration(self.a.data_dir/profile['calibration_file'])
        entries=EpisodeStore(self.a.data_dir/'episodes',cal,other).entries()
        preferred=self.a.preferences.get('last_episode_id')
        entries.sort(key=lambda row:row[1]['id']!=preferred)
        for _,episode in entries:
            safe=next((s for s in episode['steps'] if s.get('safe_boundary')=='start'),None)
            if safe:
                self.other_safe_angles=list(ModelReference(cal,profile['model_reference']).angles(safe['ticks']));return
    def reset_view(self):
        placement=self.preview_placement();self.view=(100.,-18.,.82,*DEFAULT_LOOKAT)
        if placement and placement.get('active_arm_id')=='arm3':
            import numpy as np
            from .workcell_preview import base_transform
            transform=base_transform(placement)
            center=transform[:3,:3]@np.array(DEFAULT_LOOKAT)+transform[:3,3]/1000
            # Look from the open side of the workcell, not through its backdrop.
            azimuth=placement.get('board',{}).get('yaw_deg',120.)+15.
            self.view=(azimuth,-18.,.82,*map(float,center))
        self.drag=None;self.request=None
    def rotate(self,e):
        if self.drag:
            x,y,v=self.drag;self.view=(v[0]-(e.x-x)*.5,max(-80,min(20,v[1]-(e.y-y)*.3)),*v[2:]);self.request=None
    def pan(self,e):
        if self.drag:
            x,y,v=self.drag;self.view=pan_view(v,e.x-x,e.y-y,self.canvas.winfo_height());self.request=None
    def zoom(self,f):self.view=(*self.view[:2],max(.3,min(2.,self.view[2]*f)),*self.view[3:]);self.request=None
    def state_changed(self,state):
        self.state=state;running=state in ('MIDPOINT','ANGLES','RANGE','VERIFY')
        self.s.cal_status.set(STATES.get(state,state))
        for w,enabled in [(self.start,not running),(self.choose,not running),(self.inspect_btn,not running),(self.import_btn,not running),(self.review,state=='RANGE'),(self.back,state in ('RANGE','VERIFY')),(self.save,state=='VERIFY'),(self.cancel,running)]:w.state(['!disabled'] if enabled else ['disabled'])
        self.role.configure(state='disabled' if running else 'readonly');self.port.configure(state='disabled' if running else 'normal');self.update_record_controls();self.request=None
    def update_record_controls(self):
        points=(self.last_sample or {}).get('points',{});active=self.state in ('MIDPOINT','ANGLES')
        grip=self.name()=='gripper';selected=points.get(self.name(),{});ready='zero' in selected and (not grip or selected.get('kind')=='gripper_range')
        complete=sum(points_ready(n,points.get(n,{})) for n in JOINTS)
        self.progress.set(f"기준 {sum('zero' in points.get(n,{}) for n in JOINTS)}/6 · 완료 {complete}/6")
        self.zero.configure(text='집게 −90°\n기준 기록' if grip else '선택 관절\n0° 기록')
        self.negative_label.configure(text='닫힘(최소) 틱' if grip else '음수 기준각 °');self.positive_label.configure(text='열림(최대) 틱' if grip else '양수 기준각 °')
        self.negative_input.configure(state='readonly' if grip else 'normal');self.positive_input.configure(state='readonly' if grip else 'normal')
        self.negative.configure(text='닫힘 틱 기록' if grip else '현재 틱 기록');self.positive.configure(text='열림 틱 기록' if grip else '현재 틱 기록')
        for key,label in [('negative','닫힘 기준' if grip else '음수 각도'),('zero','−90° 기준' if grip else '0°'),('positive','열림 기준' if grip else '양수 각도')]:self.guide_buttons[key].configure(text=label)
        self.rule.set('집게는 −90° 기준 자세를 2047틱으로 기록한 뒤 닫힘 최소·열림 최대 틱만 측정합니다. 각도 입력은 필요 없습니다.' if grip else '음수 틱 < 영점 틱 < 양수 틱일 때만 기록합니다. 영점을 다시 기록하면 이 관절의 양쪽 각도를 다시 측정하세요.')
        for widget,enabled in ((self.zero,active),(self.negative,active and ready),(self.positive,active and ready),(self.range,active and complete==6)):
            widget.state(['!disabled'] if enabled else ['disabled'])
    def update(self,sample):
        self.last_sample=sample
        for n,l in zip(JOINTS,LABELS):
            p=sample.get('points',{}).get(n,{})
            def point(side):
                if n=='gripper' and p.get('kind')=='gripper_range':
                    endpoint='closed' if side=='negative' else 'open';v=p.get(endpoint);return ('닫힘/' if side=='negative' else '열림/')+str(v['ticks']) if v else '—'
                v=p.get(side);return f"{v['degrees']:g}°/{v['ticks']}" if v else '—'
            self.s.cal_table.item(n,values=(l,sample['current'][n],point('negative'),p.get('zero',{}).get('ticks','—'),point('positive'),f"{sample['mins'].get(n,'—')}~{sample['maxs'].get(n,'—')}"))
        if self.name()=='gripper':self.load_degrees()
        self.update_record_controls()
    def spec(self):
        n=self.name();ref=self.a.reference;q=[ref.radians[k] for k in JOINTS];label=LABELS[JOINTS.index(n)]
        if self.preview_kind=='guide':
            if n=='gripper':
                p=(self.last_sample or {}).get('points',{}).get(n,{});anchor=p.get('zero',{}).get('ticks',2047)
                if self.guide_source=='positive' and 'open' not in p:return None,'열림 최대 틱을 기록하면 열림 기준 자세를 표시합니다.'
                tick=anchor if self.guide_source=='zero' else p.get('closed' if self.guide_source=='negative' else 'open',{}).get('ticks',anchor)
                self.guide=-90+(tick-anchor)*360/4096
            else:self.guide=0. if self.guide_source=='zero' else float(self.s.value('cal_'+self.guide_source))
            if not math.isfinite(self.guide) or abs(self.guide)>180:raise ValueError('모델 기준각은 −180°~180°로 입력하세요.')
            q[JOINTS.index(n)]+=math.radians(self.guide);caption=f'{label} · 기준 {self.guide:+g}° · 실물 구동 없음'
        else:
            w=self.s.worker
            if not w or not w.running or not self.last_sample:return None,'현재값 수신 대기 · 보정을 시작하세요.'
            ticks=self.last_sample['current'];mapping=w.mapping
            q=[ref.radians[k]+math.radians(mapping.degrees(k,ticks[k]) if mapping else (ticks[k]-2047)*360/4096) for k in JOINTS]
            if not mapping and w.points.get('gripper',{}).get('kind')=='gripper_range':q[5]=ref.radians['gripper']+math.radians(-90+(ticks['gripper']-w.points['gripper']['zero']['ticks'])*360/4096)
            caption=f'{label} · 읽은 자세 '+('3점 보정 적용' if mapping else '기본 비율 · 각도 기록 전')
            if mapping and mapping.extrapolated(n,ticks[n]):caption+=' · 기준각 바깥 추정'
        from .arm_workspace import ARM_NAMES,arm_id
        role='리더' if self.s.value('cal_role')=='리더' else ARM_NAMES[arm_id(self.a.profile)]+' 팔로워'
        config={'jigs':[],'tcp':self.a.profile['tcp'],'highlight_joint':n}
        placement=self.preview_placement()
        if placement:config['workcell']=placement
        return (q,self.view,config,self.a.profile.get('table_z_mm',-7.4)),role+' · '+caption
    def poll(self):
        if self.closed:return
        try:
            if self.a.workspace_visible and self.a.page=='settings' and self.s.tabs.select()==str(self.s.pages['calibration']):
                spec,caption=self.spec()
                self.angle_kind.set('기준 각도' if self.preview_kind=='guide' else '읽은 각도')
                if spec is None:self.angle_value.set('—')
                else:
                    degrees=self.guide if self.preview_kind=='guide' else math.degrees(spec[0][JOINTS.index(self.name())]-self.a.reference.radians[self.name()])
                    self.angle_value.set((f'{degrees:+g}°' if self.preview_kind=='guide' else f'{degrees:+.1f}°').replace('-','−'))
                    if self.preview_kind=='guide':caption=caption.replace(f' · 기준 {self.guide:+g}°','')
                self.caption.set(caption)
                rows=(self.last_sample or {}).get('verification',{}).get(self.name(),[])
                if rows:
                    v=rows[-1];self.detail.set(f"실제 {v['degrees']:g}° → 계산 {v['calculated_degrees']:.2f}°\n차이 {v['error_degrees']:+.2f}° · 검증 기록 {len(rows)}회\n같은 각도를 양쪽에서 접근해 유격도 비교하세요.")
                else:self.detail.set('서보 본체 기준으로 영점에서 회전한 각도입니다.\n청록색 관절을 보고 실물을 직접 맞춰 주세요.\n왼쪽 드래그: 회전 · 오른쪽: 이동 · 휠: 확대')
                if spec is not None and self.a.render_enabled and spec!=self.request:
                    if self.renderer is None:self.renderer=Renderer()
                    self.renderer.submit(*spec);self.request=spec
                if self.renderer:
                    item=self.renderer.poll()
                    if item and item[0]=='frame':self.image=Image.open(io.BytesIO(item[2])).copy();self.a.fit_image(self.canvas,self.image)
                    elif item and item[0]=='error':self.caption.set('3D 표시 오류: '+item[1])
        except Exception as exc:self.angle_value.set('—');self.caption.set(str(exc))
        self.job=self.a.root.after(34,self.poll)
    def close(self):
        self.closed=True;self.a.root.after_cancel(self.job)
        if self.renderer:self.renderer.close()
