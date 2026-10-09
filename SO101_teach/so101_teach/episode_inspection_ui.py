"""Episode product and selected-step inspection settings."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from .episode_inspection import STATIONS,TARGETS,EXPECTED,STATES,validate_inspection


class EpisodeInspectionPanel(ttk.Frame):
    @property
    def dirty(self):
        episode=self.app.episode
        return episode['id'] in self.product_drafts or any((episode['id'],s['id']) in self.step_drafts for s in episode['steps'])

    def __init__(self,parent,app,*,product_parent):
        super().__init__(parent,style='Card.TFrame',padding=12);self.app=app;self.loaded=None;self.loading=True;self.product_drafts={};self.step_drafts={}
        self.columnconfigure(1,weight=1)
        self.product=tk.StringVar(value='미지정');self.station=tk.StringVar(value='리니어 조립');self.target=tk.StringVar();self.expected=tk.StringVar();self.timeout=tk.StringVar(value='5');self.minimum=tk.StringVar(value='2');self.selected=tk.StringVar();self.status=tk.StringVar()
        ttk.Label(product_parent,text='에피소드 공통 · 완제품',style='Section.TLabel').grid(row=0,column=0,sticky='w',pady=5)
        ttk.Combobox(product_parent,textvariable=self.product,values=('미지정','A','B'),state='readonly',width=8).grid(row=0,column=1,sticky='w',padx=12)
        app.button(product_parent,'완제품 종류 저장',self.save_product).grid(row=0,column=2,padx=8)
        self.product_status=tk.StringVar()
        ttk.Label(product_parent,textvariable=self.product_status,style='Small.TLabel').grid(row=1,column=0,columnspan=3,sticky='w')
        ttk.Label(self,textvariable=self.selected,style='Section.TLabel',wraplength=590).grid(row=1,column=0,columnspan=3,sticky='w',pady=(16,8))
        controls=[]
        for row,(title,var,values) in enumerate((('검사 위치',self.station,tuple(STATIONS.values())),('검사 대상',self.target,()),('기대 상태',self.expected,())),2):
            ttk.Label(self,text=title).grid(row=row,column=0,sticky='w',pady=5)
            control=ttk.Combobox(self,textvariable=var,values=values,state='readonly',width=25);control.grid(row=row,column=1,columnspan=2,sticky='ew',pady=5);controls.append(control)
        self.station_choice,self.target_choice,self.expected_choice=controls
        self.station_choice.bind('<<ComboboxSelected>>',self.location_changed)
        ttk.Label(self,text='판정 기준').grid(row=5,column=0,sticky='w',pady=5)
        limits=ttk.Frame(self,style='Card.TFrame');limits.grid(row=5,column=1,columnspan=2,sticky='w')
        self.timeout_input=ttk.Spinbox(limits,textvariable=self.timeout,from_=2,to=30,width=5);self.timeout_input.pack(side='left')
        ttk.Label(limits,text='초 · 최소 판정').pack(side='left',padx=6)
        self.minimum_input=ttk.Spinbox(limits,textvariable=self.minimum,from_=2,to=10,width=5);self.minimum_input.pack(side='left')
        ttk.Label(limits,text='회').pack(side='left',padx=4)
        actions=ttk.Frame(self,style='Card.TFrame');actions.grid(row=6,column=0,columnspan=3,sticky='ew',pady=10)
        self.save_button=app.button(actions,'선택 스텝에 검사 저장',self.save_check,True);self.save_button.pack(side='left',fill='x',expand=True)
        self.remove_button=app.button(actions,'선택 스텝 검사 해제',self.remove_check);self.remove_button.pack(side='left',fill='x',expand=True,padx=(8,0))
        ttk.Label(self,textvariable=self.status,wraplength=610,style='Small.TLabel').grid(row=7,column=0,columnspan=3,sticky='w',pady=4)
        self.reset_button=app.button(self,'현재 입력 되돌리기',self.discard);self.reset_button.grid(row=9,column=0,columnspan=3,sticky='w',pady=(8,0))
        ttk.Label(self,text='팔·집게가 검사 영역을 벗어나는 스텝에 검사를 지정하세요.\n지정한 스텝 완료 후 새 영상으로 검사하며 이후 동작은 계속 진행합니다.\n불합격·판정 시간 초과 시 오류를 기록하고 자세를 유지하며 실행을 중지합니다.\n윗면 형상·위치 검사이며 내부 안착 깊이와 결합 강도는 확인하지 못합니다.',wraplength=610,style='Small.TLabel',justify='left').grid(row=8,column=0,columnspan=3,sticky='w',pady=(8,0))
        self.location_changed();self.refresh()
        self.product.trace_add('write',lambda *args:self.changed(product=True))
        for var in (self.station,self.target,self.expected,self.timeout,self.minimum):var.trace_add('write',self.changed)
    def changed(self,*args,product=False):
        if not self.loading:
            if product:self.product_drafts[self.app.episode['id']]=self.product.get()
            elif self.step() and not self.step().get('safe_boundary'):
                self.step_drafts[(self.app.episode['id'],self.app.selected)]=tuple(v.get() for v in (self.station,self.target,self.expected,self.timeout,self.minimum))
            self.update_status()
    def update_status(self):
        a=self.app;step=self.step();check=(step or {}).get('inspection');count=sum('inspection' in s for s in a.episode['steps'])
        pending=[str(i) for i,s in enumerate(a.episode['steps'],1) if (a.episode['id'],s['id']) in self.step_drafts]
        self.product_status.set('완제품 종류 변경 중 · 저장하면 에피소드 전체에 적용' if a.episode['id'] in self.product_drafts else '모든 스텝과 시작 전 검사에서 공통 사용 · 스텝마다 지정할 필요 없음')
        if self.dirty:
            changes=(['완제품 종류'] if a.episode['id'] in self.product_drafts else [])+(['스텝 '+', '.join(pending)] if pending else [])
            self.status.set('미저장: '+' · '.join(changes)+'\n화면을 옮겨도 입력 유지 · 해당 항목을 저장한 뒤 Pi로 내보내세요.')
        else:self.status.set(f'저장된 검사 {count}개 · '+('이 스텝: 검사 사용' if check else '이 스텝: 검사 없음')+'\nPC 실행은 저장 후 · Pi 실행은 내보내기 후 적용')
        current=a.episode['id'] in self.product_drafts or (a.episode['id'],a.selected) in self.step_drafts
        self.reset_button.state(['!disabled'] if current else ['disabled'])
    def discard(self):
        self.product_drafts.pop(self.app.episode['id'],None);self.step_drafts.pop((self.app.episode['id'],self.app.selected),None)
        self.loaded=None;self.refresh();self.app.notice('현재 완제품·선택 스텝 입력을 저장된 값으로 되돌렸습니다.')
    def step(self):return next((s for s in self.app.episode['steps'] if s['id']==self.app.selected),None)
    def location_changed(self,event=None):
        station=next(k for k,v in STATIONS.items() if v==self.station.get())
        self.target_choice.configure(values=tuple(TARGETS[station].values()));self.expected_choice.configure(values=tuple(EXPECTED[k] for k in STATES[station]))
        target='2' if station=='linear' and self.product.get()=='B' else next(iter(TARGETS[station]))
        self.target.set(TARGETS[station][target]);self.expected.set(EXPECTED['housing_seated' if station=='linear' else STATES[station][0]])
    def refresh(self):
        a=self.app;step=self.step()
        signature=(a.episode['id'],a.selected,(step or {}).get('name'),a.episode.get('product_type'),repr((step or {}).get('inspection')))
        if signature==self.loaded:return
        self.loaded=signature;self.loading=True
        self.product.set(self.product_drafts.get(a.episode['id'],a.episode.get('product_type','미지정')))
        self.selected.set(('안전 자세는 검사 지정 불가' if step.get('safe_boundary') else '검사할 스텝 · '+step['name']) if step else '왼쪽에서 검사할 스텝을 선택하세요.')
        allowed=bool(step and not step.get('safe_boundary'))
        check=(step or {}).get('inspection')
        for control in (self.station_choice,self.target_choice,self.expected_choice):control.configure(state='readonly' if allowed else 'disabled')
        self.timeout_input.configure(state='normal' if allowed else 'disabled');self.minimum_input.configure(state='normal' if allowed else 'disabled')
        self.save_button.state(['!disabled'] if allowed else ['disabled'])
        self.remove_button.state(['!disabled'] if allowed and check else ['disabled'])
        self.station.set('리니어 조립');self.location_changed();self.timeout.set('5');self.minimum.set('2')
        if check:
            self.station.set(STATIONS[check['station']]);self.location_changed();self.target.set(TARGETS[check['station']][check['target']]);self.expected.set(EXPECTED[check['expected']]);self.timeout.set(str(check['timeout_seconds']));self.minimum.set(str(check.get('minimum_observations',2)))
        draft=self.step_drafts.get((a.episode['id'],a.selected))
        if draft:
            self.station.set(draft[0]);self.location_changed()
            for var,value in zip((self.station,self.target,self.expected,self.timeout,self.minimum),draft):var.set(value)
        self.loading=False;self.update_status()
    def commit(self,episode,*,product=False,step=False):
        if self.app.jig_measurement_in_use():raise ValueError('실행·측정을 마친 뒤 검사 설정을 변경하세요.')
        validate_inspection(episode);self.app.store.validate(episode)
        self.app.episode=episode;self.app.save_episode()
        if product:self.product_drafts.pop(episode['id'],None)
        if step:self.step_drafts.pop((episode['id'],self.app.selected),None)
        self.loaded=None;self.app.refresh_steps();self.refresh()
    def chosen_product(self):
        product=self.product.get()
        if product not in ('A','B'):raise ValueError('완제품 종류 A 또는 B를 선택하세요.')
        return product
    def save_product(self):
        episode=deepcopy(self.app.episode)
        if self.product.get()=='미지정':episode.pop('product_type',None)
        else:episode['product_type']=self.chosen_product()
        self.commit(episode,product=True);self.app.notice('완제품 종류 저장 · Pi 내보내기 후 적용됩니다.')
    def save_check(self):
        step=self.step()
        if not step or step.get('safe_boundary'):raise ValueError('검사를 지정할 일반 스텝을 선택하세요.')
        station=next(k for k,v in STATIONS.items() if v==self.station.get())
        try:timeout=float(self.timeout.get())
        except ValueError:raise ValueError('판정 제한시간에 2~30초 사이의 숫자를 입력하세요.') from None
        check={'station':station,'target':next(k for k,v in TARGETS[station].items() if v==self.target.get()),'expected':next(k for k,v in EXPECTED.items() if v==self.expected.get()),'timeout_seconds':timeout}
        try:minimum=int(self.minimum.get())
        except ValueError:raise ValueError('최소 판정 횟수에 2~10회 정수를 입력하세요.') from None
        if minimum!=2:check['minimum_observations']=minimum
        episode=deepcopy(self.app.episode);episode['product_type']=self.chosen_product();episode['inspection_timing']='step_complete'
        next(s for s in episode['steps'] if s['id']==step['id'])['inspection']=check
        self.commit(episode,product=True,step=True);self.app.notice('완제품·스텝 안착 검사 저장 · 지정 스텝 완료 후 동작과 검사 병행')
    def remove_check(self):
        step=self.step()
        if not step:return
        episode=deepcopy(self.app.episode);next(s for s in episode['steps'] if s['id']==step['id']).pop('inspection',None)
        self.commit(episode,step=True);self.app.notice('선택 스텝의 안착 검사 해제')
