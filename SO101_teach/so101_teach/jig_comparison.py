"""Immutable teaching/run pose comparisons; never changes motion targets."""
from copy import deepcopy
import hashlib
import json
import math
import tkinter as tk
from tkinter import ttk
from .geometry import jig_delta
from .jig_heading import pose_in_reference_basis
from .jig_compatibility import execution_reference
from .display_coordinates import display_pose,display_delta


def steps_signature(steps):
    return hashlib.sha256(json.dumps(steps,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def teaching_scene_references(steps,index=0):
    """First saved pose before use, then each jig's latest visited step reference."""
    references={}
    for step in steps:
        if step.get('jig_id'):references.setdefault(step['jig_id'],step['jig_reference'])
    for step in steps[:max(0,int(index))+1]:
        if step.get('jig_id'):references[step['jig_id']]=step['jig_reference']
    return deepcopy(references)


def comparison_groups(steps,catalog,current=None):
    groups={}
    for step in steps:
        key=step.get('jig_id')
        if not key:continue
        reference=step['jig_reference']
        signature=json.dumps([key,reference],sort_keys=True)
        if signature not in groups:
            measured=(current if current and 'pose' in current else (current or {}).get(key))
            delta=None;basis=execution_reference(reference,measured,(measured or {}).get('stl_sha256'))
            if measured:
                adjusted=jig_delta(reference['pose'],pose_in_reference_basis(basis,measured),basis['symmetry_deg'])
                delta=[float(a-b) for a,b in zip(adjusted,reference['pose'])]
            groups[signature]={'jig_id':key,'jig_name':catalog.get(key,{}).get('name',key),
                'reference':deepcopy(reference),'measured':deepcopy(measured),'delta':delta,
                'distance_mm':math.hypot(*delta[:2]) if delta else None,'steps':[]}
            if basis is not reference:groups[signature]['execution_reference']=basis
        groups[signature]['steps'].append({'id':step['id'],'name':step['name']})
    return list(groups.values())


class JigComparisonPanel(ttk.Frame):
    def __init__(self,parent,*,collapsed=False,on_toggle=None):
        super().__init__(parent,style='Card.TFrame')
        self.columnconfigure(0,weight=1);self.groups=[]
        self.collapsed=bool(collapsed);self.on_toggle=on_toggle or (lambda collapsed:None)
        self.caption=tk.StringVar(value='지그 이동 비교 · 실행 전')
        ttk.Label(self,textvariable=self.caption,style='Small.TLabel',width=1).grid(row=0,column=0,sticky='ew')
        self.view=tk.StringVar(value='감지값 비교')
        self.view_choice=ttk.Combobox(self,textvariable=self.view,values=['감지값 비교','최근 실행'],state='readonly',width=9)
        self.view_choice.grid(row=0,column=1,sticky='e',padx=(5,0))
        self.toggle_button=ttk.Button(self,text='접기',style='Compact.TButton',command=self.toggle,width=8)
        self.toggle_button.grid(row=0,column=2,sticky='e',padx=(5,0))
        self.on_view=lambda:None
        self.view_choice.bind('<<ComboboxSelected>>',lambda e:self.on_view())
        self.choice=ttk.Combobox(self,state='readonly',width=25)
        self.choice.grid(row=1,column=0,columnspan=3,sticky='ew',pady=(3,3))
        self.choice.bind('<<ComboboxSelected>>',lambda e:self.draw())
        self.table=ttk.Treeview(self,style='Comparison.Treeview',columns=('axis','reference','measured','delta'),show='headings',height=3)
        for name,title,width in [('axis','항목',58),('reference','저장 초기 기준',95),('measured','실행 측정',95),('delta','보정 차이',95)]:
            self.table.heading(name,text=title);self.table.column(name,width=width,minwidth=52,anchor='e' if name!='axis' else 'w')
        self.table.grid(row=2,column=0,columnspan=3,sticky='ew')
        self.empty=ttk.Label(self,text="저장된 지그 기준이 없습니다. 지그 따라가기를 켠 스텝을 저장하면 비교할 수 있습니다.",style="Small.TLabel",wraplength=480)
        self.empty.grid(row=1,column=0,columnspan=3,sticky="ew",pady=(6,3))
        self.update_visibility()

    def update_visibility(self):
        self.toggle_button.configure(text='▸ 펼치기' if self.collapsed else '▾ 접기')
        for widget in (self.view_choice,self.choice,self.table):
            if self.collapsed or (not self.groups and widget is not self.view_choice):widget.grid_remove()
            else:widget.grid()
        if self.collapsed or self.groups:self.empty.grid_remove()
        else:self.empty.grid()
    def toggle(self):
        self.collapsed=not self.collapsed;self.update_visibility();self.on_toggle(self.collapsed)

    def show(self,groups,caption,*,live=False):
        title='확정 위치' if live else '실행 측정'
        if groups==self.groups and self.caption.get()=='지그 이동 비교 · '+caption and self.table.heading('measured','text')==title:return
        old=self.choice.current();self.groups=deepcopy(groups);self.caption.set('지그 이동 비교 · '+caption)
        self.table.heading('measured',text=title)
        self.choice.configure(values=[g['jig_name']+f" · {g['reference']['symmetry_deg']}° 대칭 · "+', '.join(s['name'] for s in g['steps']) for g in groups])
        if groups:self.grid();self.choice.current(min(max(old,0),len(groups)-1));self.draw()
        else:
            self.choice.set('')
            for row in self.table.get_children():self.table.delete(row)
        self.grid();self.update_visibility()

    def select_step(self,step_id):
        for i,g in enumerate(self.groups):
            if any(s['id']==step_id for s in g['steps']):self.choice.current(i);self.draw();break

    def draw(self):
        i=self.choice.current()
        if not 0<=i<len(self.groups):return
        g=self.groups[i]
        basis=g.get('execution_reference',g['reference'])
        reference=display_pose(basis['pose'],basis['symmetry_deg'])
        measured_pose=display_pose(pose_in_reference_basis(basis,g['measured']),basis['symmetry_deg']) if g['measured'] else None
        difference=display_delta(g['delta']) if g['delta'] is not None else None
        for axis,(name,unit) in enumerate((('X→','mm'),('Y↑','mm'),('방향','°'))):
            measured=f"{measured_pose[axis]:.1f}" if measured_pose else '—'
            delta=f"{difference[axis]:+.1f}" if difference is not None else '—'
            values=(f'{name} ({unit})',f"{reference[axis]:.1f}",measured,delta)
            if self.table.exists(str(axis)):self.table.item(str(axis),values=values)
            else:self.table.insert('','end',iid=str(axis),values=values)
