"""All stations in one live inspection view, independent of motion control."""
import json
import time
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from .ui_scroll import AutoScrollbar
import cv2
import numpy as np
from PIL import Image
from .roi_reference_ui import ROIReferencePanel
from .inspection_overlay import draw_label,detection_summary
from .inspection_geometry import all_anchors,project
from .label_layout import draw_linear_centers
from .inspection_seating import COLORS
from .adoption_display import drawable_adoptions
from .shape_inspection import InspectionWorker
from .display_coordinates import display_position,display_heading


class CameraInspectionPanel(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,style='Card.TFrame',padding=16);self.app=app;self.worker=InspectionWorker();self.last_submit=None;self.last_submit_at=0.;self.context=None;self.generation=0
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        self.mode=tk.StringVar(value='카메라 영상');self.station=tk.StringVar(value='전체 검사');self.product=tk.StringVar(value='전체');self.endpoint=tk.StringVar(value='명령 위치')
        bar=self.toolbar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=0,column=0,columnspan=2,sticky='ew');bar.columnconfigure(2,weight=1)
        choice=self.mode_choice=ttk.Combobox(bar,textvariable=self.mode,values=('카메라 영상','CAD 설명'),width=12,state='readonly');choice.grid(row=0,column=0,sticky='ns');choice.bind('<<ComboboxSelected>>',self.change)
        ttk.Label(bar,text='운반 · 리니어 · 완성품 동시 검사').grid(row=0,column=1,sticky='w',padx=16)
        ttk.Label(bar,text='리니어 위치').grid(row=0,column=3,sticky='e',padx=(16,8))
        self.endpoint_choice=ttk.Combobox(bar,textvariable=self.endpoint,values=('명령 위치','전진 목표','후진 목표'),width=11,state='readonly');self.endpoint_choice.grid(row=0,column=4,sticky='ns');self.endpoint_choice.bind('<<ComboboxSelected>>',self.change)
        self.connect_button=app.button(bar,'카메라 연결',app.start_camera);self.connect_button.grid(row=0,column=5,sticky='ns',padx=(16,0))
        self.capture_snapshot=None
        self.save_button=app.button(bar,'영상 저장',self.save_capture);self.save_button.grid(row=0,column=6,sticky='ns',padx=(8,0));self.save_button.state(['disabled'])
        self.folder_button=app.button(bar,'저장 폴더 열기',app.open_capture_folder);self.folder_button.grid(row=0,column=7,sticky='ns',padx=(8,0))
        self.status=tk.StringVar(value='영상 대기')
        status=tk.Label(self,textvariable=self.status,font=('Noto Sans CJK KR',10),fg='#526477',bg='white',height=2,width=1,anchor='w',justify='left');status.grid(row=1,column=0,columnspan=2,sticky='ew',pady=8);status.bind('<Configure>',lambda e:status.configure(wraplength=max(1,e.width)))
        self.canvas=tk.Canvas(self,bg='#132b3a',highlightthickness=0);self.canvas.grid(row=2,column=0,sticky='nsew')
        self.summary=ttk.Frame(self,padding=(8,8),style='Card.TFrame',width=520);self.summary.grid(row=2,column=1,sticky='nsew',padx=(8,0));self.summary.grid_propagate(False);self.summary.columnconfigure(0,weight=1)
        self.title=tk.StringVar(value='전체 검사 · 9개 대상')
        ttk.Label(self.summary,textvariable=self.title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.table=ttk.Treeview(self.summary,style='Inspection.Treeview',columns=('part','state','score'),show='headings',height=9,selectmode='browse')
        for key,label,width in [('part','대상',180),('state','판단',247),('score','일치도',60)]:
            self.table.heading(key,text=label);self.table.column(key,width=width,minwidth=45,stretch=key=='state')
        self.table.grid(row=1,column=0,sticky='ew',pady=(6,0))
        for key,color in [('normal','#176b35'),('abnormal','#b42324'),('outside','#8b6b00'),('waiting','#5b6470')]:self.table.tag_configure(key,foreground=color)
        self.details=tk.StringVar(value='행을 선택하면 판단 근거를 볼 수 있습니다.')
        detail_frame=ttk.Frame(self.summary,style='Card.TFrame');detail_frame.grid(row=2,column=0,sticky='ew',pady=(8,0));detail_frame.columnconfigure(0,weight=1)
        self.detail_label=tk.Text(detail_frame,font=('Noto Sans CJK KR',10),fg='#526477',bg='white',height=3,width=1,wrap='word',relief='flat',highlightthickness=0,state='disabled')
        self.detail_label.grid(row=0,column=0,sticky='ew')
        scroll=AutoScrollbar(detail_frame,orient='vertical',command=self.detail_label.yview);scroll.grid(row=0,column=1,sticky='ns');self.detail_label.configure(yscrollcommand=scroll.set)
        def fit_detail(*_):
            lines=self.detail_label.count('1.0','end-1c','displaylines')
            self.detail_label.configure(height=max(2,min(5,(lines[0] if lines else 0)+1)))
        self.detail_label.bind('<Configure>',lambda e:self.detail_label.after_idle(fit_detail))
        def show_detail(*_):
            text=self.details.get()
            if self.detail_label.get('1.0','end-1c')==text:return
            self.detail_label.configure(state='normal');self.detail_label.delete('1.0','end');self.detail_label.insert('1.0',text);self.detail_label.configure(state='disabled');self.detail_label.after_idle(fit_detail)
        self.details.trace_add('write',show_detail);show_detail()
        self.table.bind('<<TreeviewSelect>>',self.select_row);self.row_details={}
        self.position=tk.StringVar();self.parts=tk.StringVar();self.detected=tk.StringVar()
        self.legend=ttk.Label(self.summary,text='초록 정상 · 빨강 비정상 · 노랑 ROI 밖\n하늘색 점: 리니어 중심 · 완성품: 상단 결합 필수\n점선 ROI · 실선 윗면 기준 (내부 미확인)',style='Small.TLabel',wraplength=494,justify='left');self.legend.grid(row=3,column=0,sticky='ew',pady=(12,0))
        self.coords=tk.Label(self,textvariable=self.position,font=('Noto Sans CJK KR',10),fg='#526477',bg='white',height=2,width=1,anchor='nw',justify='left');self.coords.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(6,0));self.coords.bind('<Configure>',lambda e:self.coords.configure(wraplength=e.width))
        self.reference=ROIReferencePanel(self);self.reference.grid(row=2,column=0,columnspan=2,rowspan=2,sticky='nsew');self.reference.grid_remove()
        self.canvas.bind('<Configure>',lambda e:self.app.draw_camera_frame(time.monotonic()))
        self.bind('<Destroy>',lambda e:self.worker.close() if e.widget is self else None)
    def save_capture(self):
        from datetime import datetime
        from .domain import atomic_json
        snapshot=self.capture_snapshot
        if self.mode.get()!='카메라 영상' or snapshot is None or not 0<=time.monotonic()-snapshot['at']<1:
            raise ValueError('저장할 최신 검사 영상이 없습니다. 카메라 영상을 확인하세요.')
        folder=self.app.data_dir/'captures';folder.mkdir(parents=True,exist_ok=True)
        name=f"inspection_{datetime.now():%Y-%m-%d_%H-%M-%S_%f}"
        shown=folder/'화면 표시본'/(name+'.png');raw=folder/'원본'/(name+'.png');meta=folder/'판정 기록'/(name+'.json')
        shown.parent.mkdir(parents=True,exist_ok=True)
        raw.parent.mkdir(parents=True,exist_ok=True)
        try:
            if not cv2.imwrite(str(shown),snapshot['image']):raise OSError('검사 영상 저장 실패')
            if not cv2.imwrite(str(raw),snapshot['raw']):raise OSError('원본 영상 저장 실패')
            data={**snapshot['metadata'],'raw_image':str(raw.relative_to(folder)),'annotated_image':str(shown.relative_to(folder))}
            atomic_json(meta,json.loads(json.dumps(data,ensure_ascii=False,default=lambda value:value.tolist() if isinstance(value,np.ndarray) else value.item())))
        except Exception:
            for file in (shown,raw,meta):file.unlink(missing_ok=True)
            raise
        self.app.camera_save_path.set(str(shown.resolve()))
        self.app.notice('검사 영상·원본·판정 기록 저장됨 · '+str(shown.resolve()))
        return shown

    def select_row(self,event=None):
        chosen=self.table.selection()
        if chosen:
            text=self.row_details.get(chosen[0],'');self.details.set(text)
    def change(self,event=None):
        self.capture_snapshot=None;self.save_button.state(['disabled'])
        self.context=None;self.last_submit=None;self.generation+=1
        if self.mode.get()=='CAD 설명':
            self.canvas.grid_remove();self.summary.grid_remove();self.coords.grid_remove();self.reference.grid();self.status.set('CAD 기준과 조립 순서')
        else:self.reference.grid_remove();self.canvas.grid();self.summary.grid();self.coords.grid()
        self.app.draw_camera_frame(time.monotonic())
    def clear(self):
        self.previous_rows={}
        self.capture_snapshot=None;self.save_button.state(['disabled'])
        self.previous_outlines={}
        if self.context is not None:self.generation+=1
        self.context=None;self.last_submit=None;self.table.delete(*self.table.get_children());self.parts.set('');self.position.set('');self.row_details={};self.details.set('최신 영상 대기')
        self.canvas.delete('all');self.status.set('최신 카메라 영상 대기 · 이전 판단은 숨깁니다.')
    def display_rows(self,rows,context,now,result_at=None):
        # Display-only retention matches the jig view's five-second grace.
        # No retained row is supplied to an episode inspection decision.
        if getattr(self,'rows_context',None)!=context:
            self.previous_rows={};self.rows_context=context
        previous=self.previous_rows
        for key,row in rows.items():
            if row.get('state')!='checking':previous[key]=(result_at if result_at is not None else now,deepcopy(row))
        shown=deepcopy(rows);retained=False
        for key,(at,row) in list(previous.items()):
            if now-at>=5 or now<at:
                del previous[key];continue
            if key not in shown or shown[key].get('state')=='checking':
                shown[key]={**deepcopy(row),'display_retained':True,'reason':'이전 결과 · 갱신 중 · '+row.get('reason','')}
                retained=True
        return shown,retained

    def display_anchors(self,anchors,context,now):
        if getattr(self,'outlines_context',None)!=context:
            self.previous_outlines={};self.outlines_context=context
        previous=self.previous_outlines
        current={a['id'] for a in anchors}
        for anchor in anchors:
            if anchor.get('jid') and anchor.get('inspection_allowed'):
                previous[anchor['id']]=(now,deepcopy(anchor))
        shown=list(anchors)
        for key,(at,anchor) in list(previous.items()):
            if not 0<=now-at<5:
                del previous[key];continue
            if key not in current:
                shown.append({**deepcopy(anchor),'inspection_allowed':False,'source':'이전 검출 · 갱신 중'})
        return shown

    def update_frame(self,frame,results,*,detection_fresh=True,frame_at=None):
        if self.mode.get()!='카메라 영상':return
        if self.reference.data is None:self.clear();self.status.set('부품 CAD 기준 파일 확인 필요');return
        app=self.app;now=time.monotonic();frame_at=now if frame_at is None else frame_at
        adopted=drawable_adoptions(app.camera_adoption_results(now),now,app.pose_latch.seconds)
        anchors,messages=all_anchors(frame,results if detection_fresh else {},adopted,app.catalog.items,app.profile,self.reference.data,app.workcell_preview,self.endpoint.get())
        # Detected pose jitter is frame input, not a new inspection task.
        # Reset the UI worker only for an actual reference/arm/endpoint change.
        stage=(app.workcell_preview or {}).get('linear_stage',{})
        stage_context={name:stage.get(name) for name in ('position_mm','yaw_deg','endpoint_reference','alignment')}
        stage_context['state']={name:stage.get('startup_state',{}).get(name) for name in ('known','commanded_mm','moving','pending')}
        key=(self.generation,self.endpoint.get(),app.catalog.revision,json.dumps(app.profile,sort_keys=True,default=str),json.dumps(stage_context,sort_keys=True,default=str))
        if key!=self.context:self.context=key;self.last_submit=None
        if frame_at!=self.last_submit and now-self.last_submit_at>=.3:
            recovery={'catalog':app.catalog.items,'reference':self.reference.data,'placement':app.workcell_preview,'reference_directory':str(app.data_dir/'inspection_reference')}
            self.worker.submit(key,frame_at,frame,anchors,app.profile,recovery=recovery);self.last_submit=frame_at;self.last_submit_at=now
        # Nine-region inspection can finish after ~2 s on the live PC.
        # Its completed display result shares the five-second retention window.
        result=self.worker.result
        completed_at=result.get('completed',result['at']) if result else None
        valid=result and result['key']==key and 0<=now-completed_at<5
        if valid:anchors=deepcopy(result['anchors'])
        rows={r['id']:r for r in result['rows']} if valid else {};image=frame.copy();positions=[];seen=set();values=[]
        outline_context=(self.generation,self.endpoint.get(),app.catalog.revision,json.dumps(app.profile,sort_keys=True,default=str))
        rows,retained=self.display_rows(rows,outline_context,now,completed_at if valid else None)
        # Add cached regions only after submitting the fresh inspection job.
        # Retained rows must keep their matching regions during detector gaps.
        anchors=self.display_anchors(anchors,outline_context,now)
        from .label_layout import LabelLayout
        layout=LabelLayout(image.shape);labels=[]
        for a in anchors:
            try:
                layout.protect(project([[-35,-35,25],[35,-35,25],[35,35,25],[-35,35,25]],a,app.profile))
                if a.get('outline') is not None:layout.protect(a['outline'])
            except (ValueError,KeyError,cv2.error):pass
        for a in anchors:
            row=rows.get(a['id'],{'state':'checking','quality':'waiting','label':'분석 중' if a['inspection_allowed'] else '위치 확인 필요','score':None,'reason':a['source']})
            quality=row.get('quality','waiting');color=COLORS[quality]
            try:
                if a.get('pending_position'):raise ValueError('목표 위치 대기')
                polygon=np.asarray(row.get('roi_display_polygon') or project([[-33,-33,8.5],[33,-33,8.5],[33,33,8.5],[-33,33,8.5]],a,app.profile),float)
                from .vision import draw_outline
                draw_outline(image,polygon,color,dashed=True)
                top=row.get('part_top_polygon')
                if top:layout.protect(top)
                if top:cv2.polylines(image,[np.rint(top).astype(np.int32)],True,color,2,cv2.LINE_AA)
                layout.protect(polygon)
                labels.append((a['label'],polygon.mean(0)))
                jid=a['jid']
                if jid and jid not in seen:
                    seen.add(jid);outline=a.get('outline')
                    if outline is not None:cv2.polylines(image,[np.rint(outline).astype(np.int32)],True,(230,200,45),2,cv2.LINE_AA)
                    x,y=display_position(a['jig_center']);heading=display_heading(a['jig_yaw'],360 if a['station']=='운반용 지그' else 90)
                    positions.append(f'{app.catalog.items[jid]["name"]} {a["source"]}: X {x:.1f}, Y {y:.1f} mm / {heading:.1f}°')
                elif not jid:
                    x,y,_=display_position(a['origin']);positions.append(f'리니어 {a["label"]}: X {x:.1f}, Y {y:.1f} mm')
            except (ValueError,KeyError,cv2.error):pass
            label=('운반 ' if a['station']=='운반용 지그' else '리니어 ' if a['station']=='리니어 조립' else '')+a['label']
            score='—' if row['score'] is None else f'{row["score"]}%'
            values.append((a['id'],(label,row['label'],score),quality,row['reason']))
        for station in ('운반용 지그','리니어 조립','완성품 팔레트'):
            if any(a['station']==station for a in anchors):continue
            names=[g+' '+k for g in ('A','B') for k in ('하단','중단','상단')] if station=='운반용 지그' else ['팔레트 1','팔레트 2'] if station=='리니어 조립' else ['완성품 팔레트']
            for i,name in enumerate(names):values.append((station+str(i),((('운반 ' if station=='운반용 지그' else '리니어 ' if station=='리니어 조립' else '')+name),'위치 대기','—'),'waiting',messages[station]))
        # All nine targets stay visible; no station selector or product filter.
        values.sort(key=lambda v:(0 if v[1][0].startswith('운반') else 1 if v[1][0].startswith('리니어') else 2,v[1][0]))
        known=set(self.table.get_children());self.row_details={}
        for index,(iid,cells,tag,reason) in enumerate(values):
            self.row_details[iid]=cells[0]+' · '+reason
            if iid in known:self.table.item(iid,values=cells,tags=(tag,));known.remove(iid)
            else:self.table.insert('','end',iid=iid,values=cells,tags=(tag,))
            self.table.move(iid,'',index)
        for iid in known:self.table.delete(iid)
        self.table.configure(height=len(values))
        if self.table.selection():self.select_row()
        else:
            text=next((self.row_details[iid] for iid,_,tag,_ in values if tag=='abnormal'),'행을 선택하면 판단 근거를 볼 수 있습니다.')
            self.details.set(text)
        self.parts.set('\n'.join(' · '.join(cells) for _,cells,_,_ in values));self.position.set('\n'.join('  /  '.join(positions[i:i+2]) for i in range(0,len(positions),2)))
        self.detected.set(detection_summary(results,app.catalog.items,fresh=detection_fresh))
        counts={q:sum(tag==q for _,_,tag,_ in values) for q in COLORS}
        self.title.set(f'전체 {len(values)} · 정상 {counts["normal"]} / 비정상 {counts["abnormal"]} / 밖 {counts["outside"]} / 갱신 대기 {counts["waiting"]}')
        self.center_marks=draw_linear_centers(image,app.profile,self.reference.data,app.workcell_preview,self.endpoint.get(),labels=False,layout=layout)
        for label,point in labels:draw_label(image,label,point,layout=layout)
        message=messages['리니어 조립']+' · 전체 영역 자동 비교'
        if retained:message='이전 검사 결과 표시 · 갱신 중 · '+message
        if not detection_fresh:message='영상 정상 · 검출 갱신 대기 · '+message
        if valid and result.get('error'):message+=' · '+result['error']
        self.capture_snapshot={'at':frame_at,'raw':frame.copy(),'image':image.copy(),'metadata':deepcopy({'schema':1,'frame_monotonic':frame_at,'inspection_monotonic':result['at'] if valid else None,'inspection_valid':bool(valid),'display_retained':retained,'inspection_error':result.get('error') if valid else None,'rows':list(rows.values()),'display_rows':values,'anchors':anchors,'profile':app.profile,'placement':app.workcell_preview,'endpoint':self.endpoint.get(),'reference':self.reference.data})}
        self.save_button.state(['!disabled'])
        self.status.set(message);app.fit_image(self.canvas,cv2.cvtColor(image,cv2.COLOR_BGR2RGB),resample=Image.Resampling.BILINEAR)
