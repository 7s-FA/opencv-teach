"""Read each arm's own controller sample; this panel never controls devices."""
import time
import tkinter as tk
from tkinter import ttk
from .domain import JOINTS,LABELS
from .arm_workspace import arm_id,ARM_NAMES
from .telemetry import monitor_tick,monitor_load

MONITOR_GUIDE=('각도: 서보 보정 기준(보정 없음: 2047틱=0°). 부하: 1023=100%, 실제 힘의 비율 아님.\n'
 '리더: 수신 0.5초 지연 시 유지, 2초 지속 시 정지. 전압 경고만으로 연결을 끊지 않습니다.\n'
 '팔로워: 통신·모터·영점/틱 범위 오류, 목표 128틱 초과 이탈, 전압 경고·65°C 이상 5초 지속 시 중단.\n'
 '도착: 몸체 ±20틱·집게 ±30틱, 0.2초 확인. 에피소드 잔여 오차 ±32틱 이내는 정상 상태 확인 후 진행.\n'
 '화면 2초·도착 5초 지연은 이동 정지·토크 유지(에피소드 잔여 오차 진행 조건 충족 시 제외).\n'
 '전압 경고 0.1초 미만은 기록만. 지그 보정 오차 3mm·45° 초과 시 실행 전 도달 불가 표시.')

class DualMonitor(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent);self.app=app;self.cards={};self.last_at=0
        self.columnconfigure((0,1),weight=1,uniform='arms');self.rowconfigure(2,weight=1)
        for column,key in enumerate(('arm2','arm3')):
            card=app.card(self);card.grid(row=0,column=column,sticky='nsew',padx=(0,8) if column==0 else (8,0));card.columnconfigure(0,weight=1)
            title=tk.StringVar(value=ARM_NAMES[key]);status=tk.StringVar(value='연결 상태 확인 중');detail=tk.StringVar(value='')
            ttk.Label(card,textvariable=title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
            ttk.Label(card,textvariable=status,style='Small.TLabel',wraplength=420).grid(row=1,column=0,sticky='ew',pady=(6,12))
            fields=[('joint','관절',82),('ticks','현재 틱 (각도)',128),('goal','목표 틱 (각도)',128),('torque','토크',42),('voltage','전압 V',54),('temp','온도 °C',60),('load','부하 원시값 (%)',115),('current','전류 원시값',85),('status','상태',42)]
            tree=ttk.Treeview(card,columns=[k for k,_,_ in fields],show='headings',height=6,style='Monitor.Treeview',selectmode='browse')
            for k,label,width in fields:tree.heading(k,text=label);tree.column(k,width=width,minwidth=width,stretch=k in ('ticks','goal','load'),anchor='w' if k=='joint' else 'center')
            for joint,label in zip(JOINTS,LABELS):tree.insert('','end',iid=joint,values=[label,*['—']*8])
            tree.grid(row=2,column=0,sticky='ew');scroll=ttk.Scrollbar(card,orient='horizontal',command=tree.xview);scroll.grid(row=3,column=0,sticky='ew');tree.configure(xscrollcommand=scroll.set)
            def fit_table(event,tree=tree,scroll=scroll,fields=fields):
                # Fit both cards at maximum size; retain scrolling only below the readable minimum.
                available=max(1,event.width-4);minimum=sum(w for _,_,w in fields)
                extra=max(0,available-minimum);flex=('ticks','goal','load')
                for key,_,width in fields:tree.column(key,width=width+(extra//len(flex) if key in flex else 0))
                if available>=minimum:scroll.grid_remove();tree.xview_moveto(0)
                else:scroll.grid()
            tree.bind('<Configure>',fit_table)
            label=ttk.Label(card,textvariable=detail,wraplength=750,justify='left');label.grid(row=4,column=0,sticky='nw',pady=(6,2))
            connection=tk.StringVar(value='연결과 계산 기준 · 해당 팔의 작업 화면 준비 중')
            connection_label=ttk.Label(card,textvariable=connection,style='Small.TLabel',wraplength=750,justify='left');connection_label.grid(row=5,column=0,sticky='nw',pady=4)
            message=tk.StringVar(value='장비 알림 없음')
            message_label=ttk.Label(card,textvariable=message,wraplength=750,justify='left');message_label.grid(row=6,column=0,sticky='nw',pady=4)
            app.button(card,'이 팔의 진단 기록 보기',lambda key=key:self.open_diagnostics(key)).grid(row=7,column=0,sticky='w',pady=8)
            card.bind('<Configure>',lambda e,labels=(label,connection_label,message_label):[w.configure(wraplength=max(200,e.width-32)) for w in labels])
            self.cards[key]={'title':title,'status':status,'tree':tree,'detail':detail,'connection':connection,'message':message}
            tree.bind('<<TreeviewSelect>>',lambda e:self.update(force=True))
        guides=ttk.Frame(self);guides.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(12,0));guides.columnconfigure((0,1),weight=1,uniform='guide')
        lines=MONITOR_GUIDE.splitlines()
        for column,title,text in ((0,'값·수신 기준','\n'.join([lines[0],lines[1],lines[5]])),(1,'동작 중단·도착 기준','\n'.join(lines[2:5]))):
            panel=app.card(guides);panel.grid(row=0,column=column,sticky='nsew',padx=(0,8) if column==0 else (8,0));panel.columnconfigure(0,weight=1)
            ttk.Label(panel,text=title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
            guide=ttk.Label(panel,text=text,style='Small.TLabel',wraplength=750,justify='left');guide.grid(row=1,column=0,sticky='ew',pady=(6,0))
            panel.bind('<Configure>',lambda e,label=guide:label.configure(wraplength=max(200,e.width-32)))
    def open_diagnostics(self,key):
        manager=self.app.workspace_manager
        if manager:
            manager.select(key);manager.apps[key].settings.open_diagnostics()
        elif key==arm_id(self.app.profile):self.app.settings.open_diagnostics()
        else:self.app.notice('이 팔의 작업 화면이 열려 있지 않습니다.',True)
    def update(self,now=None,force=False):
        now=time.monotonic() if now is None else now
        if not force and now-self.last_at<.2:return
        self.last_at=now;a=self.app;manager=a.workspace_manager
        apps=manager.apps if manager else {arm_id(a.profile):a}
        for key,card in self.cards.items():
            source=apps.get(key);session=getattr(source,'session',None);sample=(getattr(session,'latest',None) or getattr(source,'latest',None)) if source else None
            connected=bool(session and session.running);fresh=bool(connected and sample and sample.fresh(now))
            state=getattr(session,'state','') if connected else ''
            error=getattr(session,'error',None) or getattr(getattr(source,'remote',None),'error',None)
            word=('오류' if error else '영점 불일치' if fresh and not sample.calibration_matches else {'READ_ONLY':'읽기 연결','HOLD':'자세 유지','MOVING':'이동 중','FOLLOW':'리더 따라가기','ACTIVATING':'토크 준비','FAULT':'오류'}.get(state,state or '미연결'))
            if connected and not fresh:word='수신 지연'
            card['title'].set(f'{ARM_NAMES[key]} · {word}')
            card['status'].set(str(error) if error else (f'최신 수신 · {max(0,now-sample.monotonic):.2f}초 전' if fresh else '최신 모터 수신값 없음'))
            if source:
                card['connection'].set('포트  '+str(getattr(source,'profile',{}).get('port','—'))+'\n영점 파일  '+source.calibration.sha256[:16]+'… · 모델 좌표는 실물 대조 전')
                message=getattr(source,'device_message',None)
                card['message'].set(getattr(source,'last_motion_stop',None) or (message.get() if message else '장비 알림 없음'))
            for joint,label in zip(JOINTS,LABELS):
                h=sample.telemetry.get(joint,{}) if fresh else {}
                values=(label,monitor_tick(source.calibration,joint,sample.ticks.get(joint),verified=sample.calibration_matches) if fresh else '—',monitor_tick(source.calibration,joint,h.get('goal_ticks'),verified=sample.calibration_matches) if fresh else '—','ON' if h.get('torque')==1 else 'OFF' if h.get('torque')==0 else '—',f"{h['voltage_v']:.1f}" if h.get('voltage_v') is not None else '—',h.get('temperature_c','—'),monitor_load(h.get('load_raw')),h.get('current_raw','—'),h.get('status','—'))
                if tuple(map(str,values))!=card['tree'].item(joint,'values'):card['tree'].item(joint,values=values)
            selected=card['tree'].selection()
            if selected and fresh:
                joint=selected[0];h=sample.telemetry.get(joint,{})
                card['detail'].set(f'{LABELS[JOINTS.index(joint)]} · 현재 {monitor_tick(source.calibration,joint,sample.ticks[joint],verified=sample.calibration_matches)} · 목표 {monitor_tick(source.calibration,joint,h.get("goal_ticks"),verified=sample.calibration_matches)}\n부하 {monitor_load(h.get("load_raw"))} · 전류 원시값 {h.get("current_raw","—")} · 상태 {h.get("status","—")}')
            elif selected:card['detail'].set('최신 수신값 없음 · 이전 값을 현재값으로 표시하지 않습니다.')
