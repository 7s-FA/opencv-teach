"""Notification and Pi execution settings only; no pose or step creation tools."""
from copy import deepcopy
import math
import tkinter as tk
from tkinter import ttk
from .episode_events import EVENT_LABELS,event_label,set_event

class EpisodeAdjustPanel:
    @property
    def dirty(self):return self._dirty or getattr(getattr(self,'inspection',None),'dirty',False) or getattr(getattr(self,'startup',None),'dirty',False)
    @dirty.setter
    def dirty(self,value):self._dirty=value
    def __init__(self,app,parent):
        self.app=app;self.loaded=None;self.dirty=False;self.loading=False;self.drafts={}
        parent.rowconfigure(1,weight=1);parent.columnconfigure(0,weight=1)
        self.common=ttk.Frame(parent,style='Card.TFrame',padding=(8,4));self.common.grid(row=0,column=0,sticky='ew',pady=(0,8))
        self.tabs=ttk.Notebook(parent);self.tabs.grid(row=1,column=0,sticky='nsew')
        main=ttk.Frame(self.tabs,style='Card.TFrame',padding=(8,10));self.tabs.add(main,text='완료 알림·실행 설정')
        from .episode_inspection_ui import EpisodeInspectionPanel
        self.inspection=EpisodeInspectionPanel(self.tabs,app,product_parent=self.common);self.tabs.add(self.inspection,text='스텝 도착 후 검사')
        from .startup_inspection_ui import StartupInspectionPanel
        self.startup=StartupInspectionPanel(self.tabs,app,self.inspection);self.tabs.add(self.startup,text='시작 전 검사')
        parent=main
        parent.columnconfigure((0,1),weight=1,uniform='episode_settings')
        left=ttk.Frame(parent,style='Card.TFrame');left.grid(row=0,column=0,sticky='nsew',padx=(0,18));left.columnconfigure(0,weight=1)
        right=ttk.Frame(parent,style='Card.TFrame');right.grid(row=0,column=1,sticky='nsew');right.columnconfigure(1,weight=1)
        ttk.Label(left,text='작업별 완료 알림',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.selected=tk.StringVar(value='왼쪽에서 스텝을 선택하세요.')
        ttk.Label(left,textvariable=self.selected,wraplength=280,justify='left').grid(row=1,column=0,sticky='ew',pady=12)
        self.choice=ttk.Combobox(left,textvariable=app.completion_event,values=['없음',*EVENT_LABELS.values()],state='readonly',width=15);self.choice.grid(row=2,column=0,sticky='ew')
        self.marker_button=app.button(left,'선택 스텝에 알림 저장',self.save_marker,True);self.marker_button.grid(row=3,column=0,sticky='ew',pady=8)
        ttk.Label(left,text='스텝의 실제 도착 확인 후 발생합니다.\n스텝 이름·순서가 바뀌어도 지정은 유지됩니다.',style='Small.TLabel',wraplength=280).grid(row=4,column=0,sticky='w',pady=(0,12))
        ttk.Label(left,textvariable=app.episode_policy,wraplength=280,justify='left',style='Small.TLabel').grid(row=5,column=0,sticky='nw')
        ttk.Label(right,text='Pi 실행 설정',style='Section.TLabel').grid(row=0,column=0,columnspan=2,sticky='w')
        self.values={}
        fields=[('speed','속도 · 틱/초',400,300,400),('acquisition_seconds','지그 측정 · 초',5,3,10),('acquisition_attempts','측정 횟수',3,1,5),('timeout_seconds','작업 제한 · 초',900,1,86400)]
        for row,(key,label,default,low,high) in enumerate(fields,1):
            ttk.Label(right,text=label).grid(row=row,column=0,sticky='w',pady=5)
            var=tk.StringVar(value=str(default));self.values[key]=var
            control=ttk.Combobox(right,textvariable=var,values=(300,350,400),state='readonly',width=7) if key=='speed' else ttk.Spinbox(right,textvariable=var,from_=low,to=high,width=8)
            control.grid(row=row,column=1,sticky='ew',padx=(8,0),pady=5);var.trace_add('write',self.changed)
        ttk.Label(right,text='속도·측정 설정은 선택한 팔의 A/B 작업에 공통 적용됩니다. 작업 제한시간은 내보낼 A/B 항목에 적용됩니다.\n확정한 지그 위치는 작업 종료까지 고정합니다.',style='Small.TLabel',wraplength=280).grid(row=5,column=0,columnspan=2,sticky='w',pady=10)
        self.save_button=app.button(right,'실행 설정 초안 저장',self.save_settings,True);self.save_button.grid(row=7,column=0,columnspan=2,sticky='ew')
        ttk.Label(right,text='초안 저장 후 에피소드 전송 탭에서 실행 설정 적용을 선택하세요.\n정상 종료: 안전 자세 → 5초 유지 → 토크 해제.\n리니어 목표·종료 순서는 Pi 기존 방식을 유지합니다.',style='Small.TLabel',wraplength=280).grid(row=8,column=0,columnspan=2,sticky='w',pady=12)
        self.draft_status=tk.StringVar(value='실행 설정 변경 없음')
        ttk.Label(right,textvariable=self.draft_status,style='Small.TLabel',wraplength=280).grid(row=9,column=0,columnspan=2,sticky='w')
        self.reset_button=app.button(right,'실행 설정 입력 되돌리기',self.discard);self.reset_button.grid(row=10,column=0,columnspan=2,sticky='w',pady=6)
    def changed(self,*args):
        if not self.loading:
            self.dirty=True;self.drafts[self.app.episode['id']]={key:var.get() for key,var in self.values.items()};self.update_status()
    def update_status(self):
        self.draft_status.set('미저장 입력 · 초안 저장 후 전송 탭에서 적용하세요.' if self._dirty else '실행 설정 변경 없음 · 전송 시 적용 여부 선택')
        self.reset_button.state(['!disabled'] if self._dirty else ['disabled'])
    def discard(self):
        self.drafts.pop(self.app.episode['id'],None);self.loaded=None;self.refresh();self.app.notice('실행 설정 입력을 저장된 값으로 되돌렸습니다.')
    def refresh_selected(self):
        a=self.app;step=next((s for s in a.episode['steps'] if s['id']==a.selected),None)
        allowed=bool(step and not step.get('safe_boundary'))
        self.selected.set(step['name'] if step else '왼쪽에서 스텝을 선택하세요.')
        self.choice.configure(state='readonly' if allowed else 'disabled');self.marker_button.state(['!disabled'] if allowed else ['disabled'])
        a.completion_event.set(event_label(a.episode,step['id']) if step else '없음')
        self.inspection.refresh()
        self.startup.refresh()
    def refresh(self):
        a=self.app;self.refresh_selected()
        snapshot=getattr(getattr(a,'episode_sync',None),'snapshot',None) or {}
        signature=(a.episode['id'],snapshot.get('fetched_at'))
        if signature==self.loaded:return
        self.loading=True
        try:
            settings={'speed':400,'acquisition_seconds':5,'acquisition_attempts':3,'hold_seconds':10,**snapshot.get('settings',{}),**a.episode.get('pi_execution_settings',{})}
            for key in self.values:
                if key in settings:self.values[key].set(f'{settings[key]:g}')
            recipe=next((v for v in snapshot.get('recipes',{}).values() if v['episode_id']==a.episode['id']),{})
            self.values['timeout_seconds'].set(str(a.episode.get('pi_timeout_seconds',recipe.get('timeout_seconds',900))))
            for key,value in self.drafts.get(a.episode['id'],{}).items():self.values[key].set(value)
        finally:self.loading=False
        self.loaded=signature;self.dirty=a.episode['id'] in self.drafts;self.update_status()
    def save_marker(self):
        a=self.app;step=next((s for s in a.episode['steps'] if s['id']==a.selected),None)
        if not step or step.get('safe_boundary'):raise ValueError('알림을 지정할 일반 스텝을 선택하세요.')
        set_event(a.episode,step['id'],a.completion_event.get());a.completion_changed=False
        a.refresh_steps();a.save_episode();a.notice('완료 알림 저장 · Pi 내보내기 후 적용됩니다.')
    def save_settings(self):
        try:values={key:float(var.get()) for key,var in self.values.items()}
        except ValueError:raise ValueError('속도·측정·제한시간 입력칸에 숫자를 입력하세요.') from None
        if not all(math.isfinite(v) for v in values.values()):raise ValueError('실행 설정에 유효한 숫자를 입력하세요.')
        if values['speed'] not in (300,350,400) or not 3<=values['acquisition_seconds']<=10 or not 1<=values['acquisition_attempts']<=5 or not values['acquisition_attempts'].is_integer() or not 1<=values['timeout_seconds']<=86400:raise ValueError('속도 300/350/400, 측정 3~10초, 횟수 1~5회, 제한 1~86400초입니다.')
        timeout=values.pop('timeout_seconds');values['acquisition_attempts']=int(values['acquisition_attempts'])
        # This is pre-execution measurement freshness, not an execution hold timer.
        # Preserve the Pi/episode value without exposing an unrelated editing field.
        snapshot=getattr(getattr(self.app,'episode_sync',None),'snapshot',None) or {}
        values['hold_seconds']=self.app.episode.get('pi_execution_settings',{}).get('hold_seconds',snapshot.get('settings',{}).get('hold_seconds',10.))
        a=self.app;a.episode['pi_execution_settings']=values;a.episode['pi_timeout_seconds']=timeout;a.save_episode();self.drafts.pop(a.episode['id'],None);self.dirty=False;self.update_status()
        a.notice('실행 설정 초안 저장 · 전송 탭에서 함께 적용을 선택해야 Pi에 반영됩니다.')
