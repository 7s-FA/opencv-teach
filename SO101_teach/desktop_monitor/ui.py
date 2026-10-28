"""Native work-watch view: recorded events and an existing Pi camera stream."""
import json
import queue
import subprocess
from pathlib import Path
import time
from datetime import datetime
import tkinter as tk
from tkinter import ttk
from PIL import Image,ImageTk
from so101_teach.ui_theme import COLORS
from so101_teach.ui_status import StatusText,set_text
from so101_teach.ui_scroll import AutoScrollbar
from so101_teach.pi_connection import load
from .client import WorkObserver
from .recording import PreviewRecording
from .model import current_run,summary,record_lines,adopted_outlines
from .overlays import visual_for,inspection_marks,paint_annotations,crop_evidence


class WorkMonitorPanel(ttk.Frame):
    def __init__(self,parent,data_dir,*,observer_factory=WorkObserver):
        super().__init__(parent,padding=0)
        self.data_dir=data_dir;self.factory=observer_factory;self.observer=None;self.closed=False
        self.before_connect=None;self.handoff_job=None;self.view_requested=False
        self.snapshot={};self.connected=False;self.received=None;self.frame=None;self.frame_key=None;self.paint_key=None
        self.follow=tk.BooleanVar(self,value=True);self.raw=tk.BooleanVar(self,value=False)
        self.connection=tk.StringVar(self,value='Pi 기록 연결 대기 · 조회 전용')
        self.camera_status=tk.StringVar(self,value='실행기가 제공하는 영상 대기')
        self.task=tk.StringVar(self,value='현재 작업 없음');self.progress=tk.StringVar(self,value='현재 상태: —\n마지막 완료: —')
        self.detail_title=tk.StringVar(self,value='현재 작업')
        self.fields={key:tk.StringVar(self,value='—') for key in ('command','arm','current','last')}
        self.jigs=tk.StringVar(self,value='이번 작업의 지그 채택 기록 없음')
        self.status=tk.StringVar(self,value='Pi 실행 기록 기준 · 조회 전용')
        self.history=tk.StringVar(self,value='최근 전체 기록');self.history_ids={};self.category=tk.StringVar(self,value='전체')
        self.row_key=None;self.check_key=None
        self.latest_inspection=None;self.pinned_inspection=None;self.held_inspection=None;self.hold_until=0.;self.hold_key=None
        self.pending_inspection=None;self.inspection_result_seen={};self.inspection_run_identity=None
        self.recorder=None;self.recording_reason='';self.recording_folder=Path(data_dir)/'recordings'
        self.displayed_inspection=None;self.recording_last_frame=None;self.recording_last_inspection=None
        self.preview_rate=tk.StringVar(self,value='10 FPS')
        self.recording_status=tk.StringVar(self,value='원본·검사본 동시 저장 · PC 처리')
        self.evidence_mode=tk.BooleanVar(self,value=False);self.overlays=tk.BooleanVar(self,value=True)
        self.inspection_zoom=tk.BooleanVar(self,value=True)
        self.columnconfigure(0,weight=1);self.rowconfigure(1,weight=3);self.rowconfigure(2,weight=2)
        bar=ttk.Frame(self,style='Surface.TFrame',padding=(12,6));bar.grid(row=0,column=0,sticky='ew',pady=(0,8));bar.columnconfigure(0,weight=1)
        self.connection_label=ttk.Label(bar,textvariable=self.connection,style='Muted.TLabel');self.connection_label.grid(row=0,column=0,sticky='w')
        self.connect_button=ttk.Button(bar,text='조회 연결',style='Compact.TButton',command=self.activate);self.connect_button.grid(row=0,column=1,padx=4)
        self.disconnect_button=ttk.Button(bar,text='조회 중지',style='Compact.TButton',command=lambda:self.deactivate(force=True));self.disconnect_button.grid(row=0,column=2)
        body=ttk.Frame(self);body.grid(row=1,column=0,sticky='nsew');body.rowconfigure(0,weight=1)
        body.columnconfigure(0,weight=3);body.columnconfigure(1,weight=2)
        camera=ttk.Frame(body,style='Surface.TFrame',padding=12);camera.grid(row=0,column=0,sticky='nsew',padx=(0,10));camera.columnconfigure(0,weight=1);camera.rowconfigure(1,weight=1)
        camera_bar=ttk.Frame(camera,style='Card.TFrame');camera_bar.grid(row=0,column=0,sticky='ew',pady=(0,6));camera_bar.columnconfigure(0,weight=1)
        ttk.Label(camera_bar,text='상단 카메라',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.rate_choice=ttk.Combobox(camera_bar,textvariable=self.preview_rate,values=('2 FPS','5 FPS','10 FPS'),state='readonly',width=6)
        self.rate_choice.grid(row=0,column=1,padx=(8,0));self.rate_choice.bind('<<ComboboxSelected>>',lambda e:self.change_preview_rate())
        ttk.Checkbutton(camera_bar,text='채택·검사 표시',variable=self.overlays,command=self.paint).grid(row=0,column=2,padx=8)
        self.inspection_button=ttk.Button(camera_bar,text='판정 영상',style='Compact.TButton',command=self.toggle_evidence)
        self.zoom_button=ttk.Checkbutton(camera_bar,text='검사 영역 확대',variable=self.inspection_zoom,command=self.paint)
        self.inspection_button.grid(row=0,column=4)
        self.canvas=tk.Canvas(camera,width=640,height=330,background=COLORS['viewport'],highlightthickness=0);self.canvas.grid(row=1,column=0,sticky='nsew')
        self.canvas.bind('<Configure>',lambda e:self.paint())
        StatusText(camera,self.camera_status,height=2).grid(row=2,column=0,sticky='ew',pady=(4,0))
        recording=ttk.Frame(camera,style='Card.TFrame');recording.grid(row=3,column=0,sticky='ew',pady=(4,0));recording.columnconfigure(2,weight=1)
        self.record_button=ttk.Button(recording,text='영상 녹화',style='Compact.TButton',command=self.toggle_recording)
        self.record_button.grid(row=0,column=0);self.record_button.state(['disabled'])
        self.record_folder_button=ttk.Button(recording,text='저장 폴더',style='Compact.TButton',command=self.open_recording_folder)
        self.record_folder_button.grid(row=0,column=1,padx=6)
        StatusText(recording,self.recording_status,height=1).grid(row=0,column=2,sticky='ew')
        detail=ttk.Frame(body,style='Surface.TFrame',padding=12);detail.grid(row=0,column=1,sticky='nsew');detail.columnconfigure(0,weight=1);detail.rowconfigure(6,weight=1)
        ttk.Label(detail,textvariable=self.detail_title,style='Section.TLabel').grid(row=0,column=0,sticky='w',pady=(0,8))
        facts=ttk.Frame(detail,style='Card.TFrame');facts.grid(row=1,column=0,sticky='ew');facts.columnconfigure(1,weight=1)
        for index,(key,title) in enumerate([('command','명령'),('arm','대상'),('current','현재 스텝'),('last','마지막 완료')]):
            ttk.Label(facts,text=title,width=11,font=('Noto Sans CJK KR',11)).grid(row=index,column=0,sticky='nw',pady=2)
            StatusText(facts,self.fields[key],height=1,font=('Noto Sans CJK KR',11)).grid(row=index,column=1,sticky='ew',pady=2)
        ttk.Separator(detail).grid(row=2,column=0,sticky='ew',pady=(8,6))
        ttk.Label(detail,text='지그 채택',style='Section.TLabel').grid(row=3,column=0,sticky='w',pady=(0,3))
        StatusText(detail,self.jigs,height=2,font=('Noto Sans CJK KR',11)).grid(row=4,column=0,sticky='ew')
        ttk.Label(detail,text='안착 판정',style='Section.TLabel').grid(row=5,column=0,sticky='w',pady=(7,5))
        checks=ttk.Frame(detail,style='Card.TFrame');checks.grid(row=6,column=0,sticky='nsew');checks.columnconfigure(0,weight=1);checks.rowconfigure(0,weight=1)
        ttk.Style(self).configure('WorkChecks.Treeview',rowheight=28,font=('Noto Sans CJK KR',11))
        self.checks=ttk.Treeview(checks,style='WorkChecks.Treeview',columns=('name','state','reason'),show='headings',height=3)
        detail.rowconfigure(6,minsize=124)
        for key,title,width in [('name','검사 대상',190),('state','판정',90),('reason','시점 / 사유',190)]:
            self.checks.heading(key,text=title);self.checks.column(key,width=width,minwidth=70)
        self.checks.grid(row=0,column=0,sticky='nsew');scroll=AutoScrollbar(checks,orient='vertical',command=self.checks.yview);scroll.grid(row=0,column=1,sticky='ns');self.checks.configure(yscrollcommand=scroll.set)
        for tag,color in [('통과','#12634d'),('실패',COLORS['danger']),('검사 중','#8a5c12')]:self.checks.tag_configure(tag,foreground=color)
        self.check_detail=tk.StringVar(self,value='')
        StatusText(detail,self.check_detail,height=2).grid(row=7,column=0,sticky='ew',pady=(3,0))
        self.checks.bind('<<TreeviewSelect>>',self.show_check)
        log=ttk.Frame(self,style='Surface.TFrame',padding=10);log.grid(row=2,column=0,sticky='nsew',pady=(10,0));log.columnconfigure(0,weight=1);log.rowconfigure(2,weight=1)
        toolbar=ttk.Frame(log,style='Card.TFrame');toolbar.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,6));toolbar.columnconfigure(0,weight=1)
        ttk.Label(toolbar,text='작업 기록',style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.history_choice=ttk.Combobox(toolbar,textvariable=self.history,values=['최근 전체 기록','현재 / 최근 작업'],state='readonly',width=28);self.history_choice.grid(row=0,column=1,padx=5);self.history_choice.bind('<<ComboboxSelected>>',lambda e:(self.render_records(),self.paint()))
        choice=ttk.Combobox(toolbar,textvariable=self.category,values=['전체','지그','검사','스텝','오류'],state='readonly',width=7);choice.grid(row=0,column=2,padx=5);choice.bind('<<ComboboxSelected>>',lambda e:self.render_records())
        ttk.Checkbutton(toolbar,text='새 기록 따라가기',variable=self.follow).grid(row=0,column=3,padx=8)
        self.raw_button=ttk.Button(toolbar,text='원문 보기',style='Compact.TButton',command=self.toggle_raw);self.raw_button.grid(row=0,column=4)
        columns=tk.Frame(log,background='#294552',height=27);columns.grid(row=1,column=0,sticky='ew');columns.grid_propagate(False)
        for x,title in [(10,'시간 · 출처'),(250,'작업 알림 · work_watch와 같은 요약 / 원문')]:
            tk.Label(columns,text=title,background='#294552',foreground='#d9e7ed',font=('Noto Sans CJK KR',10)).place(x=x,y=1)
        self.log=tk.Text(log,wrap='none',height=10,width=60,font=('Noto Sans Mono',11),background='#142d38',foreground='#e3edf2',insertbackground='white',relief='flat',padx=10,pady=7,state='disabled',tabs=(100,160,350))
        self.log.grid(row=2,column=0,sticky='nsew');scroll=AutoScrollbar(log,orient='vertical',command=self.log.yview);scroll.grid(row=2,column=1,sticky='ns');self.log.configure(yscrollcommand=scroll.set)
        horizontal=AutoScrollbar(log,orient='horizontal',command=self.log.xview);horizontal.grid(row=3,column=0,sticky='ew');self.log.configure(xscrollcommand=horizontal.set)
        for tag,color in [('error','#ffad9f'),('pass','#68d7d8'),('pending','#e3edf2')]:self.log.tag_configure(tag,foreground=color)
        ttk.Label(self,textvariable=self.status,style='Small.TLabel',background=COLORS['bg']).grid(row=3,column=0,sticky='w',pady=(5,0))
        self.job=self.after(100,self.poll)
        self.bind('<Destroy>',self.destroyed,add='+')

    def activate(self):
        self.view_requested=True
        if self.closed or self.observer and not self.observer.stop.is_set():return
        if self.before_connect and not self.before_connect():return
        try:
            self.observer=self.factory(load(self.data_dir));self.change_preview_rate();self.observer.start()
            set_text(self.connection,'Pi 기록 연결 중 · 조회 전용')
        except Exception as exc:set_text(self.connection,'조회 연결 불가 · '+str(exc))

    def recording_active(self):
        return bool(self.recorder and not self.recorder.stopping.is_set() and not self.recorder.done.is_set())

    def deactivate(self,*,force=False):
        self.view_requested=False
        if self.recording_active() and not force:return
        if self.handoff_job:self.after_cancel(self.handoff_job);self.handoff_job=None
        if self.observer:self.observer.close();self.observer=None
        self.hold_until=0.;self.pending_inspection=None
        self.connected=False;self.paint_key=None;self.frame=None;self.render_records();self.paint()
        set_text(self.connection,'기록 조회 중지 · 조회 전용');self.update_recording()

    def close(self):
        if self.closed:return
        self.closed=True
        self.stop_recording('화면 종료')
        if self.recorder:self.recorder.join(3)
        if self.handoff_job:self.after_cancel(self.handoff_job);self.handoff_job=None
        if self.job:self.after_cancel(self.job);self.job=None
        if self.observer:self.observer.close();self.observer=None

    def destroyed(self,event):
        if event.widget is self:self.close()

    def poll(self):
        began=time.monotonic();self.job=None
        if self.closed:return
        if self.observer:
            try:value=self.observer.results.get_nowait()
            except queue.Empty:value=None
            if value is not None:self.consume(value)
        if self.received and time.monotonic()-self.received>3:
            self.connected=False;set_text(self.connection,'Pi 기록 수신 지연 · 마지막 확인 상태를 표시합니다.')
            self.render_records()
        self.paint()
        self.update_recording()
        self.job=self.after(max(10,100-round((time.monotonic()-began)*1000)),self.poll)

    def consume(self,value):
        self.connected=value.get('connected',False)
        if not self.connected:
            set_text(self.connection,'Pi 연결 끊김 · '+value.get('error','재연결 대기'));self.render_records();self.paint_key=None;self.paint();self.update_recording();return
        self.received=value['received'];self.snapshot=value
        self.frame=value.get('frame');self.frame_key=value.get('frame_key')
        self.retain_inspection(value)
        self.render_records();self.paint();self.update_recording()
        stamp=datetime.fromtimestamp(value['at']).strftime('%H:%M:%S')
        set_text(self.status,'Pi 기록 · 마지막 수신 '+stamp+(' · 기록 읽기 오류: '+' / '.join(value.get('errors',[])) if value.get('errors') else ''))

    def render_records(self):
        snap=self.snapshot
        history={'최근 전체 기록':None,'현재 / 최근 작업':None}
        for run in snap.get('runs',[]):
            label=datetime.fromtimestamp(run['started']).strftime('%m-%d %H:%M:%S')+' · '+str(run.get('arm'))+' · '+run['run_id'][:6]
            history[label]=run['run_id']
        self.history_ids=history
        values=tuple(history)
        if tuple(self.history_choice.cget('values'))!=values:self.history_choice.configure(values=values)
        if self.history.get() not in history:self.history.set('최근 전체 기록')
        run=current_run(snap,history[self.history.get()]);info=summary(snap,run)
        if not self.connected and snap:info['phase']='마지막 확인 · '+info['phase']
        if self.connected:set_text(self.connection,'● 기록 수신 중   |   '+info['task']+'   |   '+info['phase']+'   |   조회 전용')
        color=COLORS['accent'] if self.connected else COLORS['muted']
        if self.connection_label.cget('foreground')!=color:self.connection_label.configure(foreground=color)
        set_text(self.task,info['task']+'\n'+info['phase'])
        set_text(self.progress,'현재 상태: '+info['current']+'\n마지막 완료: '+info['last'])
        set_text(self.detail_title,'현재 작업' if info['live'] else '최근 작업 기록')
        for key in self.fields:set_text(self.fields[key],info.get(key,'—'))
        def pose_text(row):
            pose=row['pose'].replace('x_mm=','X ').replace(':y_mm=',' mm · Y ').replace(':yaw_deg=',' mm · θ ')+'°'
            return row['name']+' · '+pose
        set_text(self.jigs,'\n'.join(pose_text(r) for r in info['jigs']) or '이번 작업의 지그 채택 기록 없음')
        rows=info['checks'];check_key=json.dumps(rows,ensure_ascii=False,sort_keys=True)
        if check_key!=self.check_key:
            self.check_key=check_key
            for key in self.checks.get_children():
                if int(key)>=len(rows):self.checks.delete(key)
            for index,item in enumerate(rows):
                reason=item.get('reason','');trigger=item.get('trigger','')
                if trigger:reason=trigger if reason==item['name'] or not reason else reason+' · '+trigger
                values=(item['name'],item['state'],reason);key=str(index)
                if self.checks.exists(key):
                    if self.checks.item(key,'values')!=values:self.checks.item(key,values=values,tags=(item['state'],))
                else:self.checks.insert('','end',iid=key,values=values,tags=(item['state'],))
        render=record_lines(snap,run,all_events=self.history.get()=='최근 전체 기록',
                            details=self.raw.get(),category=self.category.get())
        key=tuple(render)
        if key!=self.row_key:
            self.row_key=key;position=self.log.yview()[0];self.log.configure(state='normal');self.log.delete('1.0','end')
            for line,tone,_,_ in render:self.log.insert('end',line,tone)
            if not render:self.log.insert('end','표시할 기록이 없습니다. 관제 또는 final-action 명령을 기다립니다.\n')
            self.log.configure(state='disabled')
            if self.follow.get():self.log.see('end')
            else:self.log.yview_moveto(position)

    def toggle_raw(self):
        self.raw.set(not self.raw.get());self.raw_button.configure(text='요약 보기' if self.raw.get() else '원문 보기');self.render_records()

    @staticmethod
    def run_identity(run):
        return tuple((run or {}).get(key) for key in ('run_id','job_id','arm'))

    def retain_inspection(self,value):
        run=current_run(value);identity=self.run_identity(run)
        if self.inspection_run_identity!=identity:
            self.inspection_run_identity=identity;self.latest_inspection=None;self.pending_inspection=None
            if self.pinned_inspection:
                self.pinned_inspection=None;self.evidence_mode.set(False)
            # The already displayed result finishes its two seconds with its
            # own run/frame identity, explicitly labelled as previous evidence.
        visual=visual_for(value,run)
        if not visual or value.get('inspection_frame') is None:return
        self.latest_inspection=value
        states={r['id']:r for r in summary(value,run)['checks']}
        finished=tuple((key,states[key]['state'],states[key].get('_result_at',0))
                       for key in visual.get('frame_checks',[]) if states.get(key,{}).get('state') in ('통과','실패'))
        if not finished:return
        key=(identity,finished)
        if key in self.inspection_result_seen:return
        result_at=max(row[2] for row in finished)
        # Result freshness is independent of capture age: classification may
        # legitimately take several seconds on the exact checkpoint frames.
        age=max(0.,value.get('at',0)-result_at)+value.get('round_trip_s',0)
        if result_at and age<3:
            self.pending_inspection=(key,value)
            self.inspection_result_seen[key]=True
            while len(self.inspection_result_seen)>128:self.inspection_result_seen.pop(next(iter(self.inspection_result_seen)))

    def advance_inspection_hold(self,now):
        if self.evidence_mode.get() or now<self.hold_until or self.pending_inspection is None:return
        self.hold_key,self.held_inspection=self.pending_inspection;self.pending_inspection=None
        self.hold_until=now+2.

    def evidence_snapshot(self,run,now,*,allow_previous=False):
        candidate=self.pinned_inspection if self.evidence_mode.get() else self.held_inspection if now<self.hold_until else None
        return candidate if candidate and (allow_previous or self.run_identity(current_run(candidate))==self.run_identity(run)) else None

    def toggle_evidence(self):
        if self.evidence_mode.get():
            self.evidence_mode.set(False);self.pinned_inspection=None;self.hold_until=0.;self.pending_inspection=None
        else:
            self.pinned_inspection=self.evidence_snapshot(current_run(self.snapshot),time.monotonic(),allow_previous=True) or self.latest_inspection
            self.evidence_mode.set(True)
        self.paint()

    def live_frame_fresh(self,now):
        received=self.snapshot.get('frame_received');age=self.snapshot.get('frame_age')
        return bool(self.connected and self.frame is not None and received is not None and age is not None and 0<=age+now-received<2)

    def change_preview_rate(self):
        if self.observer:self.observer.preview_fps=int(self.preview_rate.get().split()[0])

    def toggle_recording(self):
        if self.recorder:
            self.stop_recording('직접 종료');return
        now=time.monotonic()
        if not self.live_frame_fresh(now):
            set_text(self.recording_status,'최신 실시간 영상 수신 후 녹화할 수 있습니다.');return
        self.paint();self.recording_last_frame=self.frame;self.recording_last_inspection=self.displayed_inspection
        try:
            self.recorder=PreviewRecording(self.recording_folder,now=now,fps=int(self.preview_rate.get().split()[0]));self.recording_reason=''
            self.recorder.offer(self.frame,now,inspection=self.recording_inspection);self.recorder.start();self.rate_choice.state(['disabled']);self.update_recording()
        except Exception as exc:
            self.recorder=None;set_text(self.recording_status,'녹화 시작 실패 · '+str(exc))

    def recording_inspection(self,now):
        # The UI has already selected, held and cropped this exact scene. Do not
        # choose another observation or expire its hold on the recording path.
        return self.displayed_inspection

    def stop_recording(self,reason):
        if self.recorder and not self.recorder.stopping.is_set():
            self.recording_reason=reason;self.recorder.finish(reason=reason,now=time.monotonic())

    def update_recording(self):
        now=time.monotonic();fresh=self.live_frame_fresh(now);recorder=self.recorder
        if recorder:
            if recorder.done.is_set():
                text=('녹화 저장 실패 · '+recorder.error if recorder.error else '원본·검사본 저장 완료 · '+recorder.name)
                set_text(self.recording_status,text+(' · '+self.recording_reason if self.recording_reason else ''))
                self.recorder=None;self.record_button.configure(text='영상 녹화');self.rate_choice.state(['!disabled'])
                if not self.view_requested:self.deactivate(force=True)
            elif recorder.stopping.is_set():
                self.record_button.configure(text='저장 중…');self.record_button.state(['disabled'])
                set_text(self.recording_status,'녹화 파일 저장 중 · '+self.recording_reason);return
            else:
                if fresh:
                    self.recording_last_frame=self.frame;self.recording_last_inspection=self.displayed_inspection;state='live'
                else:state='observer_stopped' if self.observer is None else 'disconnected' if not self.connected else 'waiting_frame'
                # A source gap keeps the last recorded pair on the same wall-time
                # timeline. The live viewer still marks the input as unavailable.
                image=self.recording_last_frame
                if image is not None:
                    recorder.offer(image,now,inspection=lambda _:self.recording_last_inspection,source_state=state)
                seconds=int(now-recorder.started)
                self.record_button.configure(text='녹화 종료');self.record_button.state(['!disabled'])
                set_text(self.recording_status,f'● 녹화 중 {seconds//60:02d}:{seconds%60:02d} · 원본+검사본 · {recorder.fps} FPS'+(' · 수신 대기 / 마지막 프레임 유지' if not fresh else ''));return
        self.record_button.state(['!disabled'] if fresh else ['disabled'])

    def open_recording_folder(self):
        try:
            self.recording_folder.mkdir(parents=True,exist_ok=True)
            subprocess.Popen(['xdg-open',str(self.recording_folder)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except Exception as exc:set_text(self.recording_status,'저장 폴더 열기 실패 · '+str(exc))

    def show_check(self,event=None):
        selection=self.checks.selection()
        if selection:set_text(self.check_detail,' · '.join(str(v) for v in self.checks.item(selection[0],'values')))

    def paint(self):
        if not self.canvas.winfo_exists():return
        now=time.monotonic();camera=self.snapshot.get('camera',{})
        fresh=self.live_frame_fresh(now)
        size=(max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height()))
        selected=self.history_ids.get(self.history.get());run=current_run(self.snapshot,selected)
        if selected is None and (self.canvas.winfo_ismapped() or self.recording_active()):self.advance_inspection_hold(now)
        retained=self.evidence_snapshot(run,now,allow_previous=selected is None);view=retained or self.snapshot
        view_run=current_run(view) if retained else run
        evidence=self.evidence_mode.get() or retained is not None;visual=visual_for(view,view_run)
        source=view.get('inspection_frame') if evidence and visual else self.frame if not evidence else None
        self.inspection_button.configure(text='실시간 영상' if self.evidence_mode.get() else '계속 보기' if retained else '판정 영상')
        if evidence:self.zoom_button.grid(row=0,column=3,padx=(0,8))
        else:self.zoom_button.grid_remove()
        if evidence:fresh=source is not None
        outlines=adopted_outlines(self.snapshot,run) if fresh and not evidence and self.overlays.get() else []
        checks=inspection_marks(view,view_run,evidence=evidence,now=now) if fresh and self.overlays.get() else []
        # Raw video receives only reference regions. Actual object contours stay
        # paired with the exact inspected frame in the evidence view.
        age=max(0,visual.get('age_s',0)+now-view.get('received',now)+view.get('round_trip_s',0))
        heading=((('직전 검사 결과 · '+summary(view,view_run)['arm']) if self.run_identity(view_run)!=self.run_identity(run) else '판정 영상')+' · '+str(int(age))+'초 전'+(' · 마지막 수신' if not self.connected else '')) if evidence else '실시간 · '+('채택·검사 표시' if self.overlays.get() else '원본')
        if evidence and retained and not self.evidence_mode.get():heading+=' · 2초 유지 / 계속 보기로 고정'
        if evidence:
            captured=next((row.get('captured_wall_time') for key,row in visual.get('checks',{}).items() if key in visual.get('frame_checks',[])),None)
            if captured:heading+=' · '+datetime.fromtimestamp(captured).strftime('%H:%M:%S')+' 촬영'
        key=(view.get('inspection_key') if evidence else self.frame_key,size,fresh,evidence,self.overlays.get(),heading,
             self.inspection_zoom.get(),json.dumps(outlines,sort_keys=True),json.dumps([{k:v for k,v in r.items() if k!='age_s'} for r in checks],sort_keys=True))
        if key==self.paint_key:return
        self.paint_key=key
        self.canvas.delete('annotation')
        if not fresh:
            self.canvas.delete('frame');self.canvas.delete('placeholder')
            message=('이번 작업의 판정 영상 대기\n검사기가 프레임을 처리하면 여기에 표시됩니다.' if evidence
                     else '영상 수신 끊김' if self.frame is not None else camera.get('error') or '상단 카메라 영상 대기')
            self.canvas.create_text(size[0]/2,size[1]/2,text=message,width=max(100,size[0]-40),fill=COLORS['muted'],font=('Noto Sans CJK KR',12),tags='placeholder')
            set_text(self.camera_status,'판정 영상은 실제 검사에서 수신한 프레임과 결과만 표시합니다.' if evidence else '영상 대기 · 실행기 카메라 공유')
            # A requested evidence frame may not exist yet; record the waiting
            # state rather than silently substituting a different live image.
            blank=Image.new('RGB',self.frame.size if self.frame is not None else (960,540),COLORS['viewport'])
            self.displayed_inspection={'image':blank,'source_size':blank.size,'checks':[],'outlines':[],
                                       'heading':message.replace('\n',' · '),'detail':self.camera_status.get(),'placeholder':message}
            return
        self.canvas.delete('placeholder');image=source.copy()
        source_size=visual.get('source_size',source.size) if evidence else source.size
        if evidence and self.inspection_zoom.get():
            # Crop even with annotations hidden, so the visibility toggle does
            # not unexpectedly change the operator's area of interest.
            crop_marks=checks or inspection_marks(view,view_run,evidence=True,now=now)
            image,source_size,shifted=crop_evidence(image,source_size,crop_marks)
            if checks:checks=shifted
            if crop_marks:heading+=' · 검사 영역 확대'
        recording_image=image
        scale=min(size[0]/image.width,size[1]/image.height)
        image=image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.BILINEAR)
        photo=ImageTk.PhotoImage(image,master=self);self.canvas.photo=photo
        existing=self.canvas.find_withtag('frame')
        if existing:self.canvas.itemconfigure(existing[0],image=photo);self.canvas.coords(existing[0],size[0]/2,size[1]/2)
        else:self.canvas.create_image(size[0]/2,size[1]/2,image=photo,tags='frame')
        rect=((size[0]-image.width)/2,(size[1]-image.height)/2,image.width,image.height)
        paint_annotations(self.canvas,rect,source_size,outlines,checks)
        self.canvas.tag_raise('annotation')
        if evidence:
            reasons=' / '.join(r['title']+' · '+('판정 프레임 확인 중' if r['state']=='통과' and r['tone']!='pass' else r['state'])+' · '+r['reason'] for r in checks[:2])
            set_text(self.camera_status,heading+'\n점선: 허용 영역 · 밝은 실선: 검출 윤곽 · '+(reasons or '표시할 검사 영역이 없습니다. 오른쪽 검사 기록에서 상태를 확인하세요.'))
        else:
            info=summary(self.snapshot,run)
            active=[r for r in info['checks'] if r['state'] in ('검사 중','재검사','위치 확정 대기','실패')]
            progress=' / '.join(r['name']+' · '+r['state']+(' '+r['reason'] if r['reason'].replace('/','').isdigit() else '') for r in active[:2])
            if not progress and camera.get('acquiring') and info['live'] and not info['jigs']:progress='지그 채택 중 · 위치 안정성 확인'
            if not progress:progress='판정 영상에서 실제 검출 윤곽과 사유를 확인할 수 있습니다.'
            set_text(self.camera_status,heading+' · 청록: 채택 위치 · 점선: 검사 영역\n'+progress)
        caption=self.camera_status.get().split('\n',1)
        self.displayed_inspection={'image':recording_image,'source_size':source_size,'checks':checks,'outlines':outlines,
                                   'heading':caption[0],'detail':caption[1] if len(caption)>1 else ''}


def launch(data_dir,*,minimized=False):
    from so101_teach.ui import styles,show_ready_window
    root=tk.Tk();root.withdraw();styles(root);root.title('SO-101 · 실시간 작업 · 조회 전용');root.geometry('1920x1043')
    root.columnconfigure(1,weight=1);root.rowconfigure(0,weight=1)
    side=tk.Frame(root,background=COLORS['nav'],width=184);side.grid(row=0,column=0,sticky='ns');side.pack_propagate(False)
    tk.Label(side,text='SO—101',font=('Noto Sans CJK KR',21,'bold'),fg='white',bg=COLORS['nav']).pack(pady=(24,6))
    tk.Label(side,text='실시간 작업 조회',font=('Noto Sans CJK KR',12),fg='white',bg=COLORS['nav']).pack(pady=24)
    body=ttk.Frame(root,padding=18);body.grid(row=0,column=1,sticky='nsew');body.columnconfigure(0,weight=1);body.rowconfigure(2,weight=1)
    ttk.Label(body,text='상단 카메라 · 실시간 작업',style='Title.TLabel').grid(row=0,column=0,sticky='w')
    ttk.Label(body,text='조회 전용',style='Muted.TLabel',background=COLORS['bg']).grid(row=1,column=0,sticky='w',pady=(4,10))
    panel=WorkMonitorPanel(body,data_dir);panel.grid(row=2,column=0,sticky='nsew')
    root.protocol('WM_DELETE_WINDOW',lambda:(panel.close(),root.destroy()))
    show_ready_window(root,minimized=minimized);root.after(150,panel.activate);root.mainloop()
