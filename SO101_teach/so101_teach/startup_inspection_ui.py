"""Episode-level visual conditions that must pass before any movement."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from .ui_scroll import AutoScrollbar
from .episode_inspection import STATIONS,START_TARGETS,EXPECTED,STATES,validate_check


class StartupInspectionPanel(ttk.Frame):
    @property
    def dirty(self):return self.app.episode['id'] in self.drafts

    def __init__(self,parent,app,inspection):
        super().__init__(parent,style='Card.TFrame',padding=12)
        self.app=app;self.inspection=inspection;self.drafts={};self.loaded=None;self.loading=True;self.selected=None
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        ttk.Label(self,text='에피소드 시작 조건',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        description=ttk.Label(self,text='지그 검출 직후 시작 조건을 검사하며, 모두 통과할 때까지 로봇 스텝은 대기합니다.\nPi에서는 리니어 이동과 지그 검출을 병행하며, 리니어 위 검사는 정지 후 진행합니다.\n대상을 함께 계산하며 부품별 같은 판정을 최소 횟수만큼 확인합니다. 판정 불가는 횟수에서 제외합니다.',style='Small.TLabel',wraplength=560,justify='left');description.grid(row=1,column=0,sticky='ew',pady=8)
        description.bind('<Configure>',lambda e:description.configure(wraplength=max(1,e.width)))
        listing=ttk.Frame(self);listing.grid(row=2,column=0,sticky='nsew');listing.columnconfigure(0,weight=1);listing.rowconfigure(0,weight=1)
        self.table=ttk.Treeview(listing,columns=('where','target','expected','time'),show='headings',selectmode='browse',height=4)
        for key,label,width in (('where','위치',95),('target','대상',175),('expected','시작 조건',140),('time','초 / 최소 횟수',110)):
            self.table.heading(key,text=label);self.table.column(key,width=width,minwidth=80,stretch=key in ('target','expected'))
        self.table.grid(row=0,column=0,sticky='nsew');scroll=AutoScrollbar(listing,command=self.table.yview);scroll.grid(row=0,column=1,sticky='ns');self.table.configure(yscrollcommand=scroll.set)
        self.table.bind('<<TreeviewSelect>>',self.select)
        form=ttk.Frame(self,style='Card.TFrame');form.grid(row=3,column=0,sticky='ew',pady=12);form.columnconfigure((0,1),weight=1,uniform='startup_fields')
        self.station=tk.StringVar(value='운반용 지그');self.target=tk.StringVar();self.expected=tk.StringVar();self.timeout=tk.StringVar(value='5');self.minimum=tk.StringVar(value='2')
        self.controls=[]
        for i,(title,var,values) in enumerate((('검사 위치',self.station,tuple(STATIONS.values())),('검사 대상',self.target,()),('기대 상태',self.expected,()))):
            row,column=2*(i//2),i%2
            ttk.Label(form,text=title).grid(row=row,column=column,sticky='w',padx=(0,8))
            widget=ttk.Combobox(form,textvariable=var,values=values,state='readonly',width=18);widget.grid(row=row+1,column=column,sticky='ew',padx=(0,8),pady=(4,8));self.controls.append(widget)
        self.controls[0].bind('<<ComboboxSelected>>',self.location_changed);self.controls[1].bind('<<ComboboxSelected>>',self.target_changed)
        limits=ttk.Frame(form,style='Card.TFrame');limits.grid(row=2,column=1,rowspan=2,sticky='nw')
        for col,(title,var,low,high) in enumerate((('제한 · 초',self.timeout,2,30),('최소 판정 · 회',self.minimum,2,10))):
            ttk.Label(limits,text=title).grid(row=0,column=col,sticky='w',padx=(0,12))
            ttk.Spinbox(limits,textvariable=var,from_=low,to=high,width=7).grid(row=1,column=col,sticky='w',padx=(0,12),pady=(4,8))
        actions=ttk.Frame(self,style='Card.TFrame');actions.grid(row=4,column=0,sticky='ew')
        app.button(actions,'새 검사 입력',self.new).pack(side='left',padx=(0,8))
        self.save_button=app.button(actions,'시작 조건 추가',self.save,True);self.save_button.pack(side='left',padx=(0,8))
        self.remove_button=app.button(actions,'선택 조건 삭제',self.remove);self.remove_button.pack(side='left',padx=(0,8))
        self.reset_button=app.button(actions,'입력 되돌리기',self.discard);self.reset_button.pack(side='left')
        self.status=tk.StringVar();ttk.Label(self,textvariable=self.status,style='Small.TLabel',wraplength=560).grid(row=5,column=0,sticky='w',pady=10)
        self.refresh()
        for var in (self.station,self.target,self.expected,self.timeout,self.minimum):var.trace_add('write',self.changed)

    def values(self):return tuple(v.get() for v in (self.station,self.target,self.expected,self.timeout,self.minimum))
    def changed(self,*args):
        if not self.loading:self.drafts[self.app.episode['id']]=(self.selected,self.values());self.update_status()
    def location_changed(self,event=None):
        station=next(k for k,v in STATIONS.items() if v==self.station.get())
        self.controls[1].configure(values=tuple(START_TARGETS[station].values()));self.target.set(next(iter(START_TARGETS[station].values())));self.target_changed()
    def target_changed(self,event=None):
        station=next(k for k,v in STATIONS.items() if v==self.station.get())
        target=next(k for k,v in START_TARGETS[station].items() if v==self.target.get())
        states=('present',) if target in ('all','all_products') else STATES[station]
        self.controls[2].configure(values=tuple(EXPECTED[k] for k in states));self.expected.set(EXPECTED[states[0]])
    def update_status(self):
        self.status.set('미저장 입력 · 시작 조건 추가/수정을 눌러 저장하세요. Pi에는 내보내기 후 적용됩니다.' if self.dirty else f'저장된 시작 조건 {len(self.app.episode.get("startup_inspections",[]))}개 · 조건 없음: 시작 전 검사 생략 · 실패/판정 불가 시 실행 중지')
        self.save_button.configure(text='선택 조건 수정' if self.selected is not None else '시작 조건 추가')
        self.remove_button.state(['!disabled'] if self.selected is not None else ['disabled']);self.reset_button.state(['!disabled'] if self.dirty else ['disabled'])
    def refresh(self):
        a=self.app;signature=(a.episode['id'],repr(a.episode.get('startup_inspections',[])))
        if signature==self.loaded:return
        changed_episode=self.loaded is None or self.loaded[0]!=a.episode['id'];self.loaded=signature;self.loading=True
        self.table.delete(*self.table.get_children())
        for i,check in enumerate(a.episode.get('startup_inspections',[])):
            self.table.insert('','end',iid=str(i),values=(STATIONS[check['station']],START_TARGETS[check['station']][check['target']],EXPECTED[check['expected']],f'{check["timeout_seconds"]:g}초 / {check.get("minimum_observations",2)}회'))
        if changed_episode:self.selected=None
        if self.selected is not None and not self.table.exists(str(self.selected)):self.selected=None
        draft=self.drafts.get(a.episode['id'])
        if draft:self.selected,values=draft;self.fill(values)
        elif self.selected is not None:self.fill_check(a.episode['startup_inspections'][self.selected])
        else:self.station.set('운반용 지그');self.location_changed();self.timeout.set('5');self.minimum.set('2')
        if self.selected is not None and self.table.exists(str(self.selected)):self.table.selection_set(str(self.selected))
        elif self.selected is not None:self.selected=None
        self.loading=False;self.update_status()
    def fill(self,values):
        self.station.set(values[0]);self.location_changed();self.target.set(values[1]);self.target_changed()
        for var,value in zip((self.station,self.target,self.expected,self.timeout,self.minimum),values):var.set(value)
    def fill_check(self,check):self.fill((STATIONS[check['station']],START_TARGETS[check['station']][check['target']],EXPECTED[check['expected']],str(check['timeout_seconds']),str(check.get('minimum_observations',2))))
    def select(self,event=None):
        selection=self.table.selection()
        if not selection or self.loading:return
        chosen=int(selection[0])
        if chosen==self.selected:return
        if self.dirty:
            self.table.selection_set(str(self.selected)) if self.selected is not None else self.table.selection_remove(*selection)
            self.app.notice('입력 중인 시작 조건을 저장하거나 되돌린 뒤 다른 조건을 선택하세요.',True);return
        self.selected=chosen;self.loading=True;self.fill_check(self.app.episode['startup_inspections'][chosen]);self.loading=False;self.update_status()
    def new(self):
        if self.dirty:raise ValueError('입력 중인 시작 조건을 저장하거나 되돌린 뒤 새 조건을 입력하세요.')
        self.selected=None;self.loaded=None;self.refresh()
    def discard(self):self.drafts.pop(self.app.episode['id'],None);self.loaded=(self.app.episode['id'],None);self.refresh()
    def save(self):
        station=next(k for k,v in STATIONS.items() if v==self.station.get())
        try:timeout=float(self.timeout.get())
        except ValueError:raise ValueError('자리별 판정 제한시간에 2~30초 사이의 숫자를 입력하세요.') from None
        check={'station':station,'target':next(k for k,v in START_TARGETS[station].items() if v==self.target.get()),'expected':next(k for k,v in EXPECTED.items() if v==self.expected.get()),'timeout_seconds':timeout}
        try:minimum=int(self.minimum.get())
        except ValueError:raise ValueError('최소 판정 횟수에 2~10회 정수를 입력하세요.') from None
        if minimum!=2:check['minimum_observations']=minimum
        validate_check(check,startup=True)
        episode=deepcopy(self.app.episode);episode['product_type']=self.inspection.chosen_product();checks=episode.setdefault('startup_inspections',[])
        if self.selected is None:checks.append(check)
        else:checks[self.selected]=check
        self.inspection.commit(episode,product=True);self.drafts.pop(episode['id'],None);self.loaded=None;self.refresh();self.app.notice('시작 조건 저장 · 부품별 최소 판정 충족 후 로봇 스텝 진행')
    def remove(self):
        if self.selected is None:return
        episode=deepcopy(self.app.episode);episode['startup_inspections'].pop(self.selected)
        self.inspection.commit(episode);self.drafts.pop(episode['id'],None);self.selected=None;self.loaded=None;self.refresh();self.app.notice('선택한 시작 조건 삭제')
