"""Saved action evidence alongside the live camera tools; never opens devices."""
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageTk
from .ui_theme import COLORS
from .ui_status import StatusText
from .ui_scroll import AutoScrollbar
from . import failure_archive_client

ROLES={'inspection':'검사 원본','inspection-annotated':'검사 판정 표시',
       'detection':'지그 검출 원본','detection-annotated':'지그 검출 표시',
       'jig-confirmed':'지그 확정 원본','jig-confirmed-annotated':'지그 확정 표시',
       'last-received':'마지막 수신 원본','last-received-annotated':'마지막 수신 표시'}
PHASES={'preparation':'준비','jig_detection':'지그 측정','startup_inspection':'시작 전 검사','checkpoint_inspection':'안착 검사','motion_inspection':'이동 중 검사','motion':'이동','planning':'경로 준비','reset':'안전 복귀'}


def date_text(value):
    try:return datetime.fromtimestamp(float(value)).strftime('%m-%d %H:%M:%S')
    except (TypeError,ValueError,OSError,OverflowError):return '시각 미상'


def work_text(row):
    command=row.get('command','')
    if command in ('A','B'):return command+(' 조립' if row.get('arm')=='arm2' else ' 운송' if row.get('arm')=='arm3' else '')
    return {'build_a':'A 조립','build_b':'B 조립','load_a':'A 운송','load_b':'B 운송','build_load_a':'A 연속','build_load_b':'B 연속','slide_build':'조립 위치 이동','slide_load':'하차 위치 이동'}.get(command,command or '실행')


class FailureHistoryPanel(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,padding=12,style='Card.TFrame');self.app=app
        self.rows=[];self.payload=None;self.image=None;self.photo=None;self.zoom=None;self.generation=0;self.loaded=False;self.closed=False
        self.jobs=queue.Queue(1);self.results=queue.Queue();self.stop=threading.Event()
        self.columnconfigure(1,weight=1);self.rowconfigure(2,weight=1)
        self.source=tk.StringVar(value='Pi 오류 기록' if app.remote_mode else '이 PC 오류 기록');self.arm=tk.StringVar(value='전체 팔');self.search=tk.StringVar()
        bar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,8));bar.columnconfigure(3,weight=1)
        sources=('이 PC 오류 기록',) if app.demo_mode else ('Pi 오류 기록','이 PC 오류 기록')
        self.source_choice=ttk.Combobox(bar,textvariable=self.source,values=sources,state='readonly',width=14);self.source_choice.grid(row=0,column=0,sticky='ns');self.source_choice.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        self.refresh_button=app.button(bar,'새로고침',self.refresh);self.refresh_button.grid(row=0,column=1,padx=8,sticky='ns')
        ttk.Label(bar,text='저장된 기록 · 카메라 연결 없이 조회',style='Small.TLabel').grid(row=0,column=2,sticky='w')
        self.copy_button=app.button(bar,'로그 복사',self.copy_log);self.copy_button.grid(row=0,column=4,sticky='ns');self.copy_button.state(['disabled'])
        self.status=tk.StringVar(value='오류 기록 탭을 열면 최근 기록을 불러옵니다.');StatusText(self,self.status,height=2).grid(row=1,column=0,columnspan=2,sticky='ew',pady=(0,8))
        left=ttk.Frame(self,width=366,style='Card.TFrame');left.grid(row=2,column=0,sticky='nsew',padx=(0,12));left.grid_propagate(False);left.columnconfigure(0,weight=1);left.rowconfigure(2,weight=1)
        filters=ttk.Frame(left,style='Card.TFrame');filters.grid(row=0,column=0,sticky='ew');filters.columnconfigure(2,weight=1)
        arm=ttk.Combobox(filters,textvariable=self.arm,values=('전체 팔','팔2','팔3'),state='readonly',width=8);arm.grid(row=0,column=0,sticky='ns');arm.bind('<<ComboboxSelected>>',lambda e:self.filter_rows())
        ttk.Label(filters,text='검색',style='Small.TLabel').grid(row=0,column=1,padx=(8,0))
        entry=ttk.Entry(filters,textvariable=self.search,width=16);entry.grid(row=0,column=2,sticky='ew',padx=(8,0));entry.bind('<Return>',lambda e:self.filter_rows());self.search.trace_add('write',lambda *_:self.filter_rows())
        self.count=tk.StringVar(value='날짜·작업·오류 내용 검색');ttk.Label(left,textvariable=self.count,style='Small.TLabel').grid(row=1,column=0,sticky='w',pady=8)
        self.table=ttk.Treeview(left,columns=('at','arm','work','image'),show='headings',selectmode='browse',style='Inspection.Treeview')
        for key,label,width in [('at','오류 시각',124),('arm','팔',42),('work','작업',103),('image','영상',52)]:self.table.heading(key,text=label);self.table.column(key,width=width,minwidth=35,stretch=key=='work')
        self.table.grid(row=2,column=0,sticky='nsew');scroll=AutoScrollbar(left,orient='vertical',command=self.table.yview);scroll.grid(row=2,column=1,sticky='ns');self.table.configure(yscrollcommand=scroll.set);self.table.bind('<<TreeviewSelect>>',self.select_record)
        right=ttk.Frame(self,style='Card.TFrame');right.grid(row=2,column=1,sticky='nsew');right.columnconfigure(0,weight=1);right.rowconfigure(2,weight=4);right.rowconfigure(4,weight=1)
        self.title=tk.StringVar(value='오류 기록을 선택하세요');ttk.Label(right,textvariable=self.title,style='Section.TLabel').grid(row=0,column=0,sticky='w',pady=(0,6))
        image_bar=ttk.Frame(right,style='Card.TFrame');image_bar.grid(row=1,column=0,sticky='ew',pady=(0,6));image_bar.columnconfigure(1,weight=1)
        self.image_name=tk.StringVar();self.image_choice=ttk.Combobox(image_bar,textvariable=self.image_name,state='disabled',width=25);self.image_choice.grid(row=0,column=0,sticky='ns');self.image_choice.bind('<<ComboboxSelected>>',lambda e:self.select_image())
        ttk.Label(image_bar,text='확대 후 드래그로 이동',style='Small.TLabel').grid(row=0,column=1,sticky='e',padx=10)
        for i,(text,command) in enumerate([('−',lambda:self.scale(.8)),('+',lambda:self.scale(1.25)),('맞춤',self.fit)]):app.button(image_bar,text,command).grid(row=0,column=i+2,sticky='ns',padx=(4,0))
        self.canvas=tk.Canvas(right,bg=COLORS['nav'],highlightthickness=0,width=1,height=350);self.canvas.grid(row=2,column=0,sticky='nsew');self.canvas.bind('<Configure>',lambda e:self.draw_image());self.canvas.bind('<ButtonPress-1>',lambda e:self.canvas.scan_mark(e.x,e.y));self.canvas.bind('<B1-Motion>',lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1))
        self.image_info=tk.StringVar(value='저장된 오류 이미지를 표시합니다.');StatusText(right,self.image_info,height=2).grid(row=3,column=0,sticky='ew',pady=(6,8))
        logs=ttk.Frame(right,style='Card.TFrame');logs.grid(row=4,column=0,sticky='nsew');logs.columnconfigure(0,weight=1);logs.rowconfigure(1,weight=1)
        log_bar=ttk.Frame(logs,style='Card.TFrame');log_bar.grid(row=0,column=0,sticky='ew',pady=(0,6));ttk.Label(log_bar,text='오류 · 실행 로그',style='Section.TLabel').pack(side='left')
        self.log_mode=tk.StringVar(value='요약과 실행 흐름');modes=ttk.Combobox(log_bar,textvariable=self.log_mode,values=('요약과 실행 흐름','전체 기록 JSON'),state='readonly',width=19);modes.pack(side='right');modes.bind('<<ComboboxSelected>>',lambda e:self.show_log())
        self.log=tk.Text(logs,width=1,height=6,wrap='word',font=('Noto Sans CJK KR',10),bg=COLORS['field'],fg=COLORS['ink'],relief='flat',padx=10,pady=8,state='disabled');self.log.grid(row=1,column=0,sticky='nsew');scroll=AutoScrollbar(logs,orient='vertical',command=self.log.yview);scroll.grid(row=1,column=1,sticky='ns');self.log.configure(yscrollcommand=scroll.set)
        self.thread=threading.Thread(target=self.work,name='failure-record-reader',daemon=True);self.thread.start();self.poll_job=self.after(80,self.poll)
        self.bind('<Destroy>',self.destroyed,add='+');self.draw_image()

    def activate(self):
        if not self.loaded:self.refresh()
    def enqueue(self,operation,key=None):
        self.generation+=1;number=self.generation
        from .pi_connection import load
        data=self.app.data_dir
        if data.name=='arm3-runtime':data=data.parent
        config=load(self.app.data_dir)
        if not config.get('host') and data!=self.app.data_dir:config=load(data)
        message={'operation':operation}
        if key:message['key']=key
        try:self.jobs.get_nowait()
        except queue.Empty:pass
        self.jobs.put((number,operation,data,'pi' if self.source.get()=='Pi 오류 기록' else 'local',message,config))
    def work(self):
        while not self.stop.is_set():
            try:number,operation,data,source,message,config=self.jobs.get(timeout=.2)
            except queue.Empty:continue
            try:value=failure_archive_client.fetch(data,source,message,config);error=None
            except Exception as exc:value=None;error=str(exc)
            self.results.put((number,operation,value,error))
    def poll(self):
        if self.closed:return
        try:
            while True:
                number,operation,value,error=self.results.get_nowait()
                if number!=self.generation:continue
                if error:self.status.set(error);self.title.set('기록을 불러오지 못했습니다');continue
                if operation=='list':
                    self.loaded=True;self.rows=value['records'];self.filter_rows();self.status.set(f"{self.source.get()} · 최근 최대 {value['limit']}건"+(f" · 읽지 못한 기록 {value['skipped']}건" if value['skipped'] else ''))
                    if not self.rows:self.status.set('저장된 오류 기록이 없습니다. 액션 오류 발생 후 새로고침하세요.')
                else:self.display_record(value)
        except queue.Empty:pass
        self.poll_job=self.after(80,self.poll)
    def refresh(self):
        self.loaded=False;self.rows=[];self.clear_detail();self.filter_rows();self.status.set('오류 기록을 불러오는 중…');self.enqueue('list')
    def filter_rows(self):
        if not hasattr(self,'table'):return
        selected=self.table.selection();previous=selected[0] if selected else None
        self.table.delete(*self.table.get_children());query=self.search.get().casefold();wanted={'팔2':'arm2','팔3':'arm3'}.get(self.arm.get())
        for row in self.rows:
            text=' '.join(str(row.get(k,'')) for k in ('error','command','arm','run_id'))+' '+date_text(row.get('at'))+' '+work_text(row)
            if wanted and row.get('arm')!=wanted or query and query not in text.casefold():continue
            self.table.insert('','end',iid=row['key'],values=(date_text(row.get('at')),{'arm2':'팔2','arm3':'팔3'}.get(row.get('arm'),'—'),work_text(row),str(row['image_count'])+'장' if row['image_count'] else '없음'))
        children=self.table.get_children();self.count.set(f'{len(children)}건 표시 / 최근 오류 {len(self.rows)}건')
        if previous in children:self.table.selection_set(previous)
        elif children:self.table.selection_set(children[0])
        else:self.generation+=1;self.clear_detail();self.title.set('조건에 맞는 오류 기록이 없습니다' if self.rows else '저장된 오류 기록이 없습니다')
    def select_record(self,event=None):
        selected=self.table.selection()
        if not selected:return
        key=selected[0]
        if self.payload and self.payload['key']==key:return
        self.clear_detail();self.title.set('오류 기록 불러오는 중…');self.status.set('이미지와 로그를 불러오는 중…');self.enqueue('read',key)
    def clear_detail(self):
        self.title.set('오류 기록을 선택하세요');self.payload=None;self.image=None;self.photo=None;self.image_choice.configure(values=(),state='disabled');self.image_name.set('');self.copy_button.state(['disabled']);self.image_info.set('');self.log.configure(state='normal');self.log.delete('1.0','end');self.log.configure(state='disabled');self.draw_image()
    def display_record(self,value):
        self.payload=value;self.copy_button.state(['!disabled']);row=next((r for r in self.rows if r['key']==value['key']),{})
        self.title.set(date_text(row.get('at'))+' · '+{'arm2':'팔2','arm3':'팔3'}.get(row.get('arm'),'팔 미상')+' · '+work_text(row))
        self.image_labels=[ROLES.get(i.get('role'),i['name']) for i in value['images']]
        self.image_choice.configure(values=self.image_labels,state='readonly' if self.image_labels else 'disabled')
        self.image_name.set(self.image_labels[0] if self.image_labels else '저장된 이미지 없음');self.select_image();self.show_log()
        self.status.set(' · '.join(value['warnings']) if value['warnings'] else self.source.get()+' · 저장된 오류 기록')
    def select_image(self):
        self.image=None;self.zoom=None
        if self.payload and self.payload['images']:
            try:
                index=self.image_labels.index(self.image_name.get());item=self.payload['images'][index]
                image=Image.open(io.BytesIO(base64.b64decode(item['data'],validate=True)))
                if image.width*image.height>16000000:raise ValueError('이미지 크기 제한 초과')
                self.image=image.convert('RGB');age=item.get('age')
                stamp=(self.payload['record'].get('failed_at') or 0)-(age or 0)
                detail='동일 원본 프레임에 저장된 확정 위치·판정 결과를 표시' if item.get('variant')=='annotated' else '수신 JPEG 원본 · 재인코딩 없음'
                if str(item.get('role')).startswith('last-received'):detail+=' · 오류 판정 프레임과 다를 수 있음'
                self.image_info.set(f"{ROLES.get(item.get('role'),item['name'])} · 촬영 {date_text(stamp)}"+(f" · 오류보다 {age:.2f}초 전" if isinstance(age,(int,float)) else '')+'\n'+detail)
            except Exception as exc:self.image_info.set('이미지를 표시하지 못했습니다: '+str(exc))
        elif self.payload:self.image_info.set(self.payload['record'].get('image_missing_reason') or '이 실행에 저장된 이미지가 없습니다. 아래 로그에서 오류 내용을 확인하세요.')
        self.draw_image()
    def fit(self):self.zoom=None;self.draw_image()
    def scale(self,factor):
        if self.image is not None:self.zoom=min(4.,max(.1,(self.zoom or self.fit_scale())*factor));self.draw_image()
    def fit_scale(self):return min(max(1,self.canvas.winfo_width())/self.image.width,max(1,self.canvas.winfo_height())/self.image.height)
    def draw_image(self):
        self.canvas.delete('all');width=max(1,self.canvas.winfo_width());height=max(1,self.canvas.winfo_height())
        if self.image is None:
            self.canvas.create_text(width/2,height/2,text='저장된 이미지 없음' if self.payload else '오류 기록을 선택하면 이미지가 표시됩니다',fill='#dbe3e8',font=('Noto Sans CJK KR',12),width=max(100,width-40));return
        scale=self.zoom or self.fit_scale();w,h=max(1,int(self.image.width*scale)),max(1,int(self.image.height*scale))
        self.photo=ImageTk.PhotoImage(self.image.resize((w,h),Image.Resampling.LANCZOS),master=self)
        self.canvas.create_image(max(width,w)/2,max(height,h)/2,image=self.photo);self.canvas.configure(scrollregion=(0,0,max(width,w),max(height,h)))
        if self.zoom is None:self.canvas.xview_moveto(0);self.canvas.yview_moveto(0)
    def show_log(self):
        if not self.payload:return
        p=self.payload;doc=p['record'];context=doc.get('context') or {};row=next((r for r in self.rows if r['key']==p['key']),{})
        if self.log_mode.get()=='전체 기록 JSON':text=json.dumps({k:v for k,v in p.items() if k not in ('images','key')},ensure_ascii=False,indent=2)
        else:
            lines=['오류: '+str(doc.get('error') or row.get('error') or '오류 내용 없음'),'단계: '+PHASES.get(context.get('phase'),context.get('phase') or '기록 없음')]
            if context.get('completed_step_name'):lines.append('마지막 완료 스텝: '+context['completed_step_name'])
            lines.append('실행 ID: '+str(context.get('run_id',row.get('run_id',''))))
            if doc.get('image_missing_reason'):lines.append('영상: '+doc['image_missing_reason'])
            lines+=['','실행 흐름']
            from .pi_execution import status_text
            for event in (p.get('run') or {}).get('events',[]):lines.append(date_text(event.get('at'))+'  '+status_text(str(event.get('status',''))))
            inspection=doc.get('inspection')
            if inspection:lines+=['','검사 판정',json.dumps(inspection,ensure_ascii=False,indent=2)]
            if p.get('inspection_log'):lines+=['','검사 로그',json.dumps(p['inspection_log'],ensure_ascii=False,indent=2)]
            text='\n'.join(lines)
        self.log.configure(state='normal');self.log.delete('1.0','end');self.log.insert('1.0',text);self.log.configure(state='disabled')
    def copy_log(self):
        if self.payload:self.clipboard_clear();self.clipboard_append(self.log.get('1.0','end-1c'));self.status.set('표시 중인 로그를 복사했습니다.')
    def destroyed(self,event):
        if event.widget is self:
            self.closed=True;self.stop.set();self.after_cancel(self.poll_job)
