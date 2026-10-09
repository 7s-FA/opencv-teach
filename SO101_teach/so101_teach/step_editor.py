"""Independent step editor and shared controls; no device ownership."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from .domain import JOINTS,LABELS

def compact_actions(parent):
    for child in parent.winfo_children():
        if isinstance(child,ttk.Button):
            child.configure(style='Compact.Primary.TButton' if child.cget('style')=='Primary.TButton' else 'Compact.TButton')
        elif isinstance(child,ttk.Frame):compact_actions(child)

def build_editor(self,parent):
    edit=self.card(parent);edit.configure(padding=(16,12,16,12));edit.columnconfigure(0,weight=1)
    self.edit_title=tk.StringVar(value='스텝 자세 편집');ttk.Label(edit,textvariable=self.edit_title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
    self.edit_hint=tk.StringVar(value='실물 조정은 별도로 켜서 사용합니다.');hint=ttk.Label(edit,textvariable=self.edit_hint,style='Small.TLabel');hint.grid(row=1,column=0,sticky='w')
    self.step_name=tk.StringVar(value='자세 1')
    name_row=ttk.Frame(edit,style='Card.TFrame');name_row.grid(row=2,column=0,sticky='ew',pady=(0,4));name_row.columnconfigure(1,weight=1)
    ttk.Label(name_row,text='이름',style='Small.TLabel').grid(row=0,column=0,padx=(0,8))
    self.step_name_entry=ttk.Entry(name_row,textvariable=self.step_name,width=18);self.step_name_entry.grid(row=0,column=1,sticky='ew')
    self.follow_jig=tk.BooleanVar(value=False)
    settings=ttk.Frame(edit,style='Card.TFrame');settings.grid(row=3,column=0,sticky='ew')
    self.follow_jig_check=ttk.Checkbutton(settings,text='지그 따라가기',style='Jig.TCheckbutton',variable=self.follow_jig,command=self.update_jig_hint);self.follow_jig_check.grid(row=0,column=0,sticky='ew',pady=(2,0))
    settings.columnconfigure(1,weight=1);self.step_jig_choice=ttk.Combobox(settings,state='readonly',width=12,values=[d['name'] for d in self.catalog.items.values()]);self.step_jig_choice.current(0);self.step_jig_choice.grid(row=0,column=1,sticky='ew',padx=(5,0));self.step_jig_choice.bind('<<ComboboxSelected>>',lambda e:self.update_jig_hint())
    self.jig_hint=tk.StringVar(value='보정 안 함 · 저장한 모터 틱 그대로 실행')
    self.jig_hint_label=ttk.Label(settings,textvariable=self.jig_hint,style='Small.TLabel',wraplength=235);self.jig_hint_label.grid(row=1,column=0,columnspan=2,sticky='w',pady=(1,1))
    settings.bind('<Configure>',lambda e:self.jig_hint_label.configure(wraplength=max(100,e.width)))
    self.completion_event=tk.StringVar(value='없음');self.completion_changed=False
    edit.rowconfigure(4,weight=1)
    self.joint_controls=group=ttk.Frame(edit,style='Card.TFrame')
    group.grid(row=4,column=0,sticky='nsew');group.columnconfigure(0,weight=1)
    for i,(n,label) in enumerate(zip(JOINTS,LABELS)):
        box=ttk.Frame(group,style='Card.TFrame');box.grid(row=i,column=0,sticky='new',pady=(0,4 if i<len(JOINTS)-1 else 0));box.columnconfigure(0,weight=1)
        current=tk.StringVar(value=f'{label} · 현재 —');self.current_vars[n]=current
        ttk.Label(box,textvariable=current,font=('Noto Sans CJK KR',10,'bold')).grid(row=0,column=0,sticky='w')
        var=tk.StringVar(value=str(self.target[n]));self.tick_vars[n]=var;m=self.calibration.motors[n]
        spin=ttk.Spinbox(box,style='Joint.TSpinbox',from_=m.low,to=m.high,increment=1,textvariable=var,width=7,command=lambda n=n:self.input_tick(n));self.spins[n]=spin;spin.grid(row=0,column=1,padx=(8,0));spin.bind('<Return>',lambda e,n=n:self.input_tick(n));spin.bind('<FocusOut>',lambda e,n=n:self.input_tick(n))
        slider=ttk.Scale(box,from_=m.low,to=m.high,command=lambda v,n=n:self.slider_tick(n,v));self.sliders[n]=slider
        slider.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(3,0))

    def fit_joint_spacing(event):
        rows=group.winfo_children()
        # Use spare height for visible separation, without pushing actions out.
        gap=max(4,min(16,(event.height-sum(row.winfo_reqheight() for row in rows))//(len(rows)-1)))
        for row in rows[:-1]:
            if tuple(row.grid_info()['pady'])!=(0,gap):row.grid_configure(pady=(0,gap))
    group.bind('<Configure>',fit_joint_spacing)

    actions=ttk.Frame(edit,style='Card.TFrame');actions.grid(row=5,column=0,sticky='ew',pady=(4,0))
    actions.columnconfigure((0,1),weight=1,uniform='edit_actions')
    self.shortcut_hint=ttk.Label(actions,text='Space 실물 저장 · Z 지그 전환',style='Small.TLabel')
    self.shortcut_hint.grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,2))
    self.capture_btn=self.button(actions,'실물 자세 추가 · Space',self.capture);self.capture_btn.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(0,2));self.capture_btn.state(['disabled'])
    app=getattr(self,'app',self)
    self.capture_btn.bind('<KeyPress-space>',app.space_capture)
    self.capture_btn.bind('<KeyRelease-space>',app.space_key_release)
    self.add_step_btn=self.button(actions,'새 스텝 추가',self.commit_target,True);self.add_step_btn.grid(row=2,column=0,sticky='ew',padx=(0,4),pady=2)
    self.update_step_btn=self.button(actions,'선택 스텝 수정',self.update_selected_step);self.update_step_btn.grid(row=2,column=1,sticky='ew',padx=(4,0),pady=2)
    self.button(actions,'실물값 불러오기',self.copy_live).grid(row=3,column=0,sticky='ew',padx=(0,4),pady=(2,0))
    self.move_btn=self.button(actions,'실물 조정 켜기',lambda:app.live_adjust.toggle(self),True);self.move_btn.grid(row=3,column=1,sticky='ew',padx=(4,0),pady=(2,0))
    compact_actions(actions)
    def compact_height(event):
        if event.widget!=edit:return
        small=event.height<620
        if small:
            hint.grid_remove();self.shortcut_hint.grid_remove()
        else:
            hint.grid();self.shortcut_hint.grid()
        for row in group.winfo_children():row.grid_configure(pady=(0,1 if small else 4))
    edit.bind('<Configure>',compact_height,add='+')
    self.editor_card=edit
    app.install_teaching_shortcuts(edit)
    return edit

class SecondEditor:
    """Owns draft/selection only. Shared episode and execution services stay on App."""
    def __init__(self,app):
        self.app=app;self.selected=None;self.episode_id=app.episode['id']
        self.target=app.reference.middle.copy();self.spins={};self.tick_vars={};self.current_vars={};self.sliders={};self.set_guard=False
        self.editor_card=build_editor(self,app.workspace)
        self.editor_card.grid(row=0,column=1,sticky='nsew',padx=(0,12))
        self.edit_title.set('스텝 자세 편집 2')
        self.edit_hint.set('같은 스텝·목록 밖 우클릭: 닫기')
        self.shortcut_hint.configure(text='Space 실물 저장 · Z 지그 전환')
        self.apply_target(self.target);self.poll()
    @property
    def calibration(self):return self.app.calibration
    @property
    def catalog(self):return self.app.catalog
    @property
    def episode(self):return self.app.episode
    def card(self,*args,**kwargs):return self.app.card(*args,**kwargs)
    def button(self,*args,**kwargs):return self.app.button(*args,**kwargs)
    def selected_jig_id(self):
        keys=list(self.catalog.items);return keys[max(0,min(self.step_jig_choice.current(),len(keys)-1))]
    def set_step_jig(self,key):
        keys=list(self.catalog.items)
        if key in keys:self.step_jig_choice.current(keys.index(key))
    def ensure_teaching_reference(self):return self.app.ensure_teaching_reference(self.selected_jig_id())
    def current_jig_reference(self):return self.app.current_jig_reference(self.selected_jig_id())
    def teaching_jig_hold_active(self):return self.app.teaching_jig_hold_active()
    def update_jig_hint(self):type(self.app).update_jig_hint(self)
    def activate(self):
        self.app.preview_jig_references=None;self.app.measured_target_active=False
        self.app.preview_editor=2
        if self.app.live_adjust.owner is not self:self.app.set_mode('target')
        self.app.last_render=None
    def load(self,key):
        self.app.live_adjust.stop()
        step=next(s for s in self.episode['steps'] if s['id']==key)
        self.selected=key;self.original=deepcopy(step)
        from .episode_events import event_label
        self.completion_event.set(event_label(self.episode,key));self.completion_changed=False
        self.step_name.set(step['name']);self.follow_jig.set(bool(step.get('jig_id')));self.set_step_jig(step.get('jig_id'))
        self.update_jig_hint();self.app.stop_preview(quiet=True);self.apply_target(step['ticks']);self.activate();self.poll()
    def apply_target(self,ticks):
        self.target=self.calibration.ticks(ticks);self.set_guard=True
        try:
            for n,v in self.target.items():self.tick_vars[n].set(str(v));self.sliders[n].set(v)
        finally:self.set_guard=False
    def input_tick(self,name):
        if self.set_guard:return
        try:
            value=int(self.tick_vars[name].get())
            if self.app.live_adjust.edit(self,name,value):return
            new={**self.target,name:value};self.calibration.ticks(new)
        except ValueError as exc:self.tick_vars[name].set(str(self.target[name]));self.app.notice(str(exc),True);return
        self.app.stop_preview(quiet=True);self.apply_target(new);self.activate()
    def slider_tick(self,name,value):
        if self.set_guard:return
        if self.app.live_adjust.owner is self:return
        self.app.live_adjust.stop()
        self.app.stop_preview(quiet=True);self.apply_target({**self.target,name:round(float(value))});self.activate()
    def with_jig(self,step,original=None):
        if self.follow_jig.get():
            key=self.selected_jig_id()
            reference=(original.get('jig_reference') if original and original.get('jig_id')==key else self.app.new_step_jig_reference(key))
            if not reference:raise ValueError('지그 따라가기: 지그 다시 읽기로 먼저 위치를 확인하세요.')
            step.update(jig_id=key,jig_reference=deepcopy(reference))
        return step
    def edited_step(self,*,existing=False):
        ticks={n:int(v.get()) for n,v in self.tick_vars.items()};self.apply_target(ticks)
        return self.with_jig(self.app.store.step(ticks,self.step_name.get()),original=self.original if existing else None)
    def save(self,step,update=False):
        if self.episode_id!=self.episode['id']:raise ValueError('에피소드가 바뀌었습니다. 스텝을 다시 선택하세요.')
        if update:
            index=next((i for i,s in enumerate(self.episode['steps']) if s['id']==self.selected),None)
            if index is None or self.episode['steps'][index].get('safe_boundary'):raise ValueError('안전 자세는 안전 자세 설정에서 변경하세요.')
            if self.episode['steps'][index]!=self.original:raise ValueError('다른 편집창에서 수정된 스텝입니다. 편집 2를 닫고 다시 우클릭해 불러오세요.')
            step={**deepcopy(self.original),**step,'id':self.selected}
        from .episode_events import events_for,set_event
        self.episode.setdefault('completion_events',events_for(self.episode))
        if self.completion_changed:set_event(self.episode,step['id'],self.completion_event.get())
        self.completion_changed=False
        if update:self.episode['steps'][index]=step
        elif not self.app.append_step(step):return
        self.step_name.set(step['name'])
        self.selected=step['id'];self.original=deepcopy(step)
        self.app.prune_unused_teaching_references();self.app.refresh_steps();self.app.save_episode();self.update_jig_hint();self.activate()
        self.app.notice('편집 2 · '+('선택 스텝을 수정했습니다.' if update else '새 스텝을 추가했습니다.'))
    def commit_target(self):self.save(self.edited_step())
    def update_selected_step(self):self.save(self.edited_step(existing=True),update=True)
    def copy_live(self):
        self.app.live_adjust.stop()
        if not self.app.latest or not self.app.latest.fresh():raise ValueError('최신 실물값이 없습니다. 팔로워를 연결하세요.')
        self.app.stop_preview(quiet=True);self.apply_target(self.app.latest.ticks);self.activate()
    def capture(self):
        latest=self.app.latest
        if not latest:raise ValueError('팔로워 연결 후 저장하세요.')
        step=self.app.store.step(latest.ticks,self.step_name.get(),{'kind':'demo'}) if latest.role=='demo' else self.app.store.capture(latest,self.step_name.get())
        self.save(self.with_jig(step));self.apply_target(step['ticks'])
    def execute_target(self):
        step=self.edited_step(existing=True);self.activate();self.app.prepare_execution('move',[step])
    def poll(self):
        a=self.app;fresh=bool(a.session and a.session.running and a.latest and a.latest.fresh() and a.latest.calibration_matches)
        for n,label in zip(JOINTS,LABELS):self.current_vars[n].set(f'{label} · 현재 {a.latest.ticks[n]}' if fresh else f'{label} · 현재 —')
        self.capture_btn.state(['!disabled'] if fresh else ['disabled'])
        selected=next((s for s in self.episode['steps'] if s['id']==self.selected),None)
        self.update_step_btn.state(['!disabled'] if selected and not selected.get('safe_boundary') else ['disabled'])
        self.move_btn.state(['!disabled'] if fresh and (a.live_adjust.owner is self or getattr(a.session,'state','')=='HOLD' and not a.pending_execution) else ['disabled'])
