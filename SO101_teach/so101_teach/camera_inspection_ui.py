"""All stations in one live inspection view, independent of motion control."""
import json
import time
from copy import deepcopy
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from .ui_status import StatusText,set_text
from .ui_scroll import AutoScrollbar
from .ui_theme import COLORS as UI_COLORS
import cv2
import numpy as np
from PIL import Image
from .roi_reference_ui import ROIReferencePanel
from .inspection_overlay import detection_summary
from .part_labels import PartLabels
from .inspection_geometry import all_anchors,project
from .label_layout import draw_linear_centers
from .inspection_seating import COLORS
from .adoption_display import drawable_adoptions
from .shape_inspection import InspectionWorker
from .display_coordinates import display_position,display_heading
from .ai_inspection import METHODS,inspection_settings,inspection_label
from .inference_protocol import confidence_percent


def _same_values(a,b):
    """Exact comparison, including arrays; no rounded pose or time cache keys."""
    if isinstance(a,np.ndarray) or isinstance(b,np.ndarray):
        return isinstance(a,np.ndarray) and isinstance(b,np.ndarray) and a.dtype==b.dtype and a.shape==b.shape and np.array_equal(a,b)
    if type(a) is not type(b):return False
    if isinstance(a,dict):return a.keys()==b.keys() and all(_same_values(a[k],b[k]) for k in a)
    if isinstance(a,(list,tuple)):return len(a)==len(b) and all(_same_values(x,y) for x,y in zip(a,b))
    return a==b


class CameraInspectionPanel(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,style='Surface.TFrame',padding=16);self.app=app;self.worker=InspectionWorker();self.last_submit=None;self.last_submit_at=0.;self.context=None;self.generation=0;self.part_labels=PartLabels()
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        self.mode=tk.StringVar(value='카메라 영상');self.station=tk.StringVar(value='전체 검사');self.product=tk.StringVar(value='전체');self.endpoint=tk.StringVar(value='명령 위치')
        bar=self.toolbar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=0,column=0,columnspan=2,sticky='ew');bar.columnconfigure(2,weight=1)
        choice=self.mode_choice=ttk.Combobox(bar,textvariable=self.mode,values=('카메라 영상','CAD 설명'),width=12,state='readonly');choice.grid(row=0,column=0,sticky='ns');choice.bind('<<ComboboxSelected>>',self.change)
        ttk.Label(bar,text='운반 · 리니어 · 완성품 동시 검사').grid(row=0,column=1,sticky='w',padx=16)
        ttk.Label(bar,text='리니어 위치').grid(row=0,column=3,sticky='e',padx=(16,8))
        self.endpoint_choice=ttk.Combobox(bar,textvariable=self.endpoint,values=('명령 위치','조립 위치','하차 위치'),width=11,state='readonly');self.endpoint_choice.grid(row=0,column=4,sticky='ns');self.endpoint_choice.bind('<<ComboboxSelected>>',self.change)
        self.connect_button=app.button(bar,'카메라 연결',app.start_camera);self.connect_button.grid(row=0,column=5,sticky='ns',padx=(16,0))
        self.capture_snapshot=None
        self.save_button=app.button(bar,'영상 저장',self.save_capture);self.save_button.grid(row=0,column=6,sticky='ns',padx=(8,0));self.save_button.state(['disabled'])
        self.folder_button=app.button(bar,'저장 폴더 열기',app.open_capture_folder);self.folder_button.grid(row=0,column=7,sticky='ns',padx=(8,0))
        method_bar=ttk.Frame(bar,style='Card.TFrame');method_bar.grid(row=1,column=0,columnspan=8,sticky='ew',pady=(10,0))
        ttk.Label(method_bar,text='안착검사 방식').pack(side='left',padx=(0,8))
        self.method=tk.StringVar(value=inspection_label(app.profile))
        self.method_choice=ttk.Combobox(method_bar,textvariable=self.method,values=tuple(METHODS),state='readonly',width=17)
        self.method_choice.pack(side='left',padx=(0,8));self.method_choice.bind('<<ComboboxSelected>>',lambda e:self.update_method_controls())
        ttk.Label(method_bar,text='최소 신뢰도 (50~90%)').pack(side='left',padx=(0,5))
        self.confidence=tk.StringVar(value=f"{inspection_settings(app.profile)['confidence']*100:g}")
        self.confidence_entry=ttk.Spinbox(method_bar,from_=50,to=90,increment=1,width=5,textvariable=self.confidence)
        self.confidence_entry.configure(validate='key',validatecommand=(self.register(lambda s:s=='' or s.isascii() and s.isdecimal()),'%P'))
        self.confidence_entry.pack(side='left',padx=(0,8));self.confidence_entry.bind('<Return>',lambda e:app.guard(self.apply_method))
        self.method_apply=app.button(method_bar,'검사 방식 적용',self.apply_method);self.method_apply.pack(side='left',padx=(0,16))
        self.method_status=tk.StringVar()
        self.method_status_label=ttk.Label(method_bar,textvariable=self.method_status,style='Small.TLabel');self.method_status_label.pack(side='left')
        self.update_method_controls(sync=True)
        self.confidence.trace_add('write',lambda *args:self.update_method_controls())
        self.status=tk.StringVar(value='영상 대기')
        status=tk.Label(self,textvariable=self.status,font=('Noto Sans CJK KR',10),fg='#526477',bg=UI_COLORS['panel'],height=2,width=1,anchor='w',justify='left');status.grid(row=1,column=0,columnspan=2,sticky='ew',pady=8);status.bind('<Configure>',lambda e:status.configure(wraplength=max(1,e.width)))
        self.canvas=tk.Canvas(self,bg=UI_COLORS['viewport'],highlightthickness=0);self.canvas.grid(row=2,column=0,sticky='nsew')
        self.summary=ttk.Frame(self,padding=(8,8),style='Card.TFrame',width=520);self.summary.grid(row=2,column=1,sticky='nsew',padx=(8,0));self.summary.grid_propagate(False);self.summary.columnconfigure(0,weight=1)
        self.title=tk.StringVar(value='전체 검사 · 9개 대상')
        ttk.Label(self.summary,textvariable=self.title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
        self.table=ttk.Treeview(self.summary,style='Inspection.Treeview',columns=('part','state','score'),show='headings',height=9,selectmode='browse')
        for key,label,width in [('part','대상',180),('state','판단',211),('score','형상 일치도',96)]:
            self.table.heading(key,text=label);self.table.column(key,width=width,minwidth=45,stretch=key=='state')
        self.table.grid(row=1,column=0,sticky='ew',pady=(6,0))
        for key,color in [('normal','#176b35'),('abnormal','#b42324'),('outside','#8b6b00'),('waiting','#5b6470')]:self.table.tag_configure(key,foreground=color)
        self.details=tk.StringVar(value='행을 선택하면 판단 근거를 볼 수 있습니다.')
        detail_frame=ttk.Frame(self.summary,style='Card.TFrame');detail_frame.grid(row=2,column=0,sticky='ew',pady=(8,0));detail_frame.columnconfigure(0,weight=1)
        self.detail_area=StatusText(detail_frame,self.details,height=3)
        self.detail_area.grid(row=0,column=0,sticky='ew');self.detail_label=self.detail_area.text
        self.table.bind('<<TreeviewSelect>>',self.select_row);self.row_details={}
        self.position=tk.StringVar();self.parts=tk.StringVar();self.detected=tk.StringVar()
        self.legend=ttk.Label(self.summary,text='초록 정상 · 빨강 비정상 · 노랑 ROI 밖\n하늘색 점: 리니어 중심 · 완성품: 상단 결합 필수\n점선 ROI · 실선 윗면 기준 (내부 미확인)',style='Small.TLabel',wraplength=494,justify='left');self.legend.grid(row=3,column=0,sticky='ew',pady=(12,0))
        self.coords=tk.Label(self,textvariable=self.position,font=('Noto Sans CJK KR',10),fg='#526477',bg=UI_COLORS['panel'],height=2,width=1,anchor='nw',justify='left');self.coords.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(6,0));self.coords.bind('<Configure>',lambda e:self.coords.configure(wraplength=e.width))
        self.reference=ROIReferencePanel(self);self.reference.grid(row=2,column=0,columnspan=2,rowspan=2,sticky='nsew');self.reference.grid_remove()
        self.canvas.bind('<Configure>',lambda e:self.app.draw_camera_frame(time.monotonic()))
        self.bind('<Destroy>',lambda e:self.worker.close() if e.widget is self else None)
    def update_method_controls(self,sync=False):
        settings=inspection_settings(self.app.profile);label=inspection_label(self.app.profile)
        if sync:
            self.method.set(label)
            self.confidence.set(f"{settings['confidence']*100:g}")
        self.confidence_entry.configure(state='normal' if self.method.get()=='AI 라벨 + CAD' else 'disabled')
        detail='형상·색상 비교' if settings['method']=='opencv' else f"YOLO26{settings['model']} · {settings['confidence']*100:g}% · CAD 안착"
        set_text(self.method_status,'적용 중: '+label+' · '+detail)
        try:unchanged=confidence_percent(self.confidence.get())==settings['confidence']
        except ValueError:unchanged=False
        self.method_apply.state(['disabled'] if self.method.get()==label and (settings['method']=='opencv' or unchanged) else ['!disabled'])

    def apply_method(self):
        from .model_regions import runtime,identity
        from .pi_execution import active_execution
        app=self.app
        if (app.pending_execution or app.camera_task or app.safe_entry or app.playing or app.detector.frozen
                or app.pose_latch.frozen or getattr(getattr(app,'inspection_run',None),'busy',False) or active_execution(app)):
            raise ValueError('측정·검사·실행이 끝난 뒤 안착검사 방식을 적용하세요.')
        settings=inspection_settings(app.profile)
        settings['method']=METHODS[self.method.get()]
        if settings['method']=='model_roi':
            settings['confidence']=confidence_percent(self.confidence.get())
            settings['model_identity']=identity(runtime(settings['model'])[2])
        profile={**app.profile,'part_inspection':settings};inspection_settings(profile)
        if app.remote_mode and app.remote:
            if app.remote.error:raise ValueError('Pi 통신을 복구한 뒤 검사 방식을 적용하세요.')
            from .remote_config import configuration_bundle
            bundle=configuration_bundle(app);bundle['profile']['part_inspection']=settings
            app.remote.sync_bundle(bundle)
        app.profile=profile;app.persist_configuration()
        self.clear();self.generation+=1;self.last_submit_at=0.
        self.title.set('전체 검사 · 새 영상 대기');self.update_method_controls(sync=True)
        app.notice('안착검사 방식 저장: '+inspection_label(profile)+' · 새 영상부터 검사합니다.')
        app.draw_camera_frame(time.monotonic())

    def save_capture(self):
        from datetime import datetime
        from .domain import atomic_json
        snapshot=self.capture_snapshot
        if self.mode.get()!='카메라 영상' or snapshot is None or not 0<=time.monotonic()-snapshot['at']<1:
            raise ValueError('저장할 최신 검사 영상이 없습니다. 카메라 영상을 확인하세요.')
        self.app.camera_gallery.require_capture_ready(snapshot['at'])
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
        self.app.camera_gallery.capture_saved(snapshot['at'])
        self.app.camera_save_path.set(str(shown.resolve()))
        self.app.camera_gallery.refresh(preferred=shown.name)
        self.app.notice('검사 영상·원본·판정 기록 저장됨 · '+str(shown.resolve()))
        return shown

    def select_row(self,event=None):
        chosen=self.table.selection()
        if chosen:
            text=self.row_details.get(chosen[0],'');set_text(self.details,text)
    def change(self,event=None):
        self.capture_snapshot=None;self.save_button.state(['disabled'])
        self.context=None;self.last_submit=None;self.generation+=1
        if self.mode.get()=='CAD 설명':
            self.canvas.grid_remove();self.summary.grid_remove();self.coords.grid_remove();self.reference.grid();set_text(self.status,'CAD 기준과 조립 순서')
        else:self.reference.grid_remove();self.canvas.grid();self.summary.grid();self.coords.grid()
        self.app.draw_camera_frame(time.monotonic())
    def clear(self):
        self.previous_rows={}
        self.capture_snapshot=None;self.save_button.state(['disabled'])
        self.previous_outlines={}
        if self.context is not None:self.generation+=1
        self.context=None;self.last_submit=None
        children=self.table.get_children()
        if children:self.table.delete(*children)
        set_text(self.title,'영상 대기 · 검사 결과 없음')
        set_text(self.parts,'');set_text(self.position,'');self.row_details={};set_text(self.details,'최신 영상 대기')
        if self.canvas.find_all():self.canvas.delete('all')
        set_text(self.status,'최신 카메라 영상 대기 · 이전 판단은 숨깁니다.')
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

    def update_frame(self,frame,results,*,detection_fresh=True,frame_at=None,inference=None):
        if self.mode.get()!='카메라 영상':return
        if self.reference.data is None:self.clear();set_text(self.status,'부품 CAD 기준 파일 확인 필요');return
        app=self.app;now=time.monotonic();frame_at=now if frame_at is None else frame_at
        adopted=drawable_adoptions(app.camera_adoption_results(now),now,app.pose_latch.seconds)
        # Remaining-time labels are not geometry; expiry already removed them
        # above. All pose, calibration and placement values remain exact keys.
        anchor_inputs=(frame.shape,results if detection_fresh else {},
                       {k:{n:v for n,v in row.items() if n!='hold_remaining_s'} for k,row in adopted.items()},
                       app.catalog.items,app.profile,self.reference.data,app.workcell_preview,self.endpoint.get())
        cached=getattr(self,'_anchor_cache',None)
        if cached is None or cached[0]!=frame_at or not _same_values(cached[1],anchor_inputs):
            anchors,messages=all_anchors(frame,*anchor_inputs[1:])
            self._anchor_cache=(frame_at,deepcopy(anchor_inputs),deepcopy(anchors),dict(messages))
        else:anchors,messages=deepcopy(cached[2]),dict(cached[3])
        profile_cache=getattr(self,'_profile_cache',None)
        if profile_cache is None or not _same_values(profile_cache[0],app.profile):
            profile_cache=(deepcopy(app.profile),json.dumps(app.profile,sort_keys=True,default=str));self._profile_cache=profile_cache
        profile_key=profile_cache[1]
        # Detected pose jitter is frame input, not a new inspection task.
        # Reset the UI worker only for an actual reference/arm/endpoint change.
        stage=(app.workcell_preview or {}).get('linear_stage',{})
        stage_context={name:stage.get(name) for name in ('position_mm','yaw_deg','endpoint_reference','alignment')}
        stage_context['state']={name:stage.get('startup_state',{}).get(name) for name in ('known','commanded_mm','moving','pending')}
        key=(self.generation,self.endpoint.get(),app.catalog.revision,profile_key,json.dumps(stage_context,sort_keys=True,default=str))
        if key!=self.context:self.context=key;self.last_submit=None
        if frame_at!=self.last_submit and now-self.last_submit_at>=.3:
            recovery={'catalog':app.catalog.items,'reference':self.reference.data,'placement':app.workcell_preview,'reference_directory':str(app.data_dir/'inspection_reference')}
            self.worker.submit(key,frame_at,frame,anchors,app.profile,recovery=recovery,inference=inference);self.last_submit=frame_at;self.last_submit_at=now
        # Nine-region inspection can finish after ~2 s on the live PC.
        # Its completed display result shares the five-second retention window.
        result=self.worker.result
        completed_at=result.get('completed',result['at']) if result else None
        valid=result and result['key']==key and 0<=now-completed_at<5
        if valid:anchors=deepcopy(result['anchors'])
        rows={r['id']:r for r in result['rows']} if valid else {};image=frame.copy();positions=[];seen=set();values=[]
        outline_context=(self.generation,self.endpoint.get(),app.catalog.revision,profile_key)
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
                labels.append({'id':a['id'],'text':a['label'],'polygon':polygon,'top':top or [],'product':a.get('product')})
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
            values.append((a['id'],(label,row['label'],score),quality,inspection_label(app.profile)+' · '+row['reason']))
        for station in ('운반용 지그','리니어 조립','완성품 팔레트'):
            if any(a['station']==station for a in anchors):continue
            names=[g+' '+k for g in ('A','B') for k in ('하단','중단','상단')] if station=='운반용 지그' else ['팔레트 1','팔레트 2'] if station=='리니어 조립' else ['완성품 팔레트']
            for i,name in enumerate(names):values.append((station+str(i),((('운반 ' if station=='운반용 지그' else '리니어 ' if station=='리니어 조립' else '')+name),'위치 대기','—'),'waiting',messages[station]))
        # All nine targets stay visible; no station selector or product filter.
        values.sort(key=lambda v:(0 if v[1][0].startswith('운반') else 1 if v[1][0].startswith('리니어') else 2,v[1][0]))
        known=set(self.table.get_children());self.row_details={}
        for index,(iid,cells,tag,reason) in enumerate(values):
            self.row_details[iid]=cells[0]+' · '+reason
            if iid in known:
                if self.table.item(iid,'values')!=tuple(map(str,cells)) or self.table.item(iid,'tags')!=(tag,):
                    self.table.item(iid,values=cells,tags=(tag,))
                known.remove(iid)
            else:self.table.insert('','end',iid=iid,values=cells,tags=(tag,))
            if self.table.index(iid)!=index:self.table.move(iid,'',index)
        for iid in known:self.table.delete(iid)
        if int(self.table.cget('height'))!=len(values):self.table.configure(height=len(values))
        if self.table.selection():self.select_row()
        else:
            text=next((self.row_details[iid] for iid,_,tag,_ in values if tag=='abnormal'),'행을 선택하면 판단 근거를 볼 수 있습니다.')
            set_text(self.details,text)
        set_text(self.parts,'\n'.join(' · '.join(cells) for _,cells,_,_ in values));set_text(self.position,'\n'.join('  /  '.join(positions[i:i+2]) for i in range(0,len(positions),2)))
        set_text(self.detected,detection_summary(results,app.catalog.items,fresh=detection_fresh))
        counts={q:sum(tag==q for _,_,tag,_ in values) for q in COLORS}
        set_text(self.title,f'{inspection_label(app.profile)} · 전체 {len(values)} · 정상 {counts["normal"]} / 비정상 {counts["abnormal"]} / 밖 {counts["outside"]} / 대기 {counts["waiting"]}')
        self.center_marks=draw_linear_centers(image,app.profile,self.reference.data,app.workcell_preview,self.endpoint.get(),labels=False,layout=layout)
        self.label_rectangles=self.part_labels.draw(image,labels)
        message=messages['리니어 조립']
        # Presentation wording must not change the signed inspection algorithm.
        if message.startswith('리니어 고정 좌표 · '):
            from .linear_state import direction_label,position_label
            stage=(app.workcell_preview or {}).get('linear_stage',{});state=stage.get('startup_state',{})
            commanded_positions=[stage.get('endpoint_reference',{}).get(key,{}).get('commanded_mm') for key in ('forward','retracted')]
            commanded_positions += [state.get('commanded_mm'),state.get('last_commanded_mm')]
            for mm in commanded_positions:
                if type(mm) in (int,float):message=message.replace(direction_label(stage,mm)+' 목표',position_label(stage,mm))
            message=message.removesuffix(' · 실제 위치 피드백 없음')
        message+=' · 전체 영역 자동 비교'
        if retained:message='이전 검사 결과 표시 · 갱신 중 · '+message
        if not detection_fresh:message='영상 정상 · 검출 갱신 대기 · '+message
        if valid and result.get('error'):message+=' · '+result['error']
        raw=getattr(self,'_capture_raw',None)
        if raw is None or not _same_values(raw,frame):self._capture_raw=frame.copy()
        # image belongs to this render only; neither fit_image nor save mutates it.
        self.capture_snapshot={'at':frame_at,'raw':self._capture_raw,'image':image,'metadata':deepcopy({'schema':1,'demo_replay':bool(getattr(app,'demo_mode',False)),'demo_scene':getattr(app,'demo_scene',None),'label_layout':self.label_rectangles,'frame_monotonic':frame_at,'inspection_monotonic':result['at'] if valid else None,'inspection_valid':bool(valid),'display_retained':retained,'inspection_error':result.get('error') if valid else None,'rows':list(rows.values()),'display_rows':values,'anchors':anchors,'profile':app.profile,'placement':app.workcell_preview,'endpoint':self.endpoint.get(),'reference':self.reference.data})}
        blocked=bool(self.app.camera_gallery.capture_blocked(frame_at))
        if self.save_button.instate(['disabled'])!=blocked:self.save_button.state(['disabled'] if blocked else ['!disabled'])
        set_text(self.status,message);app.fit_image(self.canvas,cv2.cvtColor(image,cv2.COLOR_BGR2RGB),resample=Image.Resampling.BILINEAR)


class CaptureGalleryPanel(ttk.Frame):
    """Live raw frame and local capture pairs; never changes detection or motion."""
    def __init__(self,parent,app):
        super().__init__(parent,padding=12)
        self.app=app;self.folder=app.data_dir/'captures';self.entries={};self.selected=None
        self.live_key=None;self.saved_image=None;self.live_image=None;self.last_scan=0
        self.last_capture_at=None;self.last_capture_source=None;self.capture_ready_at=0.
        self.delete_ready_at=0.
        self.variant=tk.StringVar(self,value='원본');self.saved_status=tk.StringVar(self,value='저장된 사진이 없습니다.')
        self.live_status=tk.StringVar(self,value='최신 카메라 영상 대기');self.count=tk.StringVar(self,value='촬영 목록 · 0장')
        self.columnconfigure(0,weight=1);self.rowconfigure(1,weight=1)
        bar=ttk.Frame(self);bar.grid(row=0,column=0,sticky='ew',pady=(0,10))
        ttk.Style(self).configure('Capture.Shell.Primary.TButton',font=('Noto Sans CJK KR',18,'bold'),padding=(32,18))
        self.save_button=app.button(bar,'사진 촬영·저장',self.capture,True);self.save_button.configure(style='Capture.Shell.Primary.TButton',width=16);self.save_button.pack(side='left',padx=(0,8));self.save_button.state(['disabled'])
        self.save_button.bind('<Button-3>',self.delete_click)
        hint=ttk.Frame(bar,width=440,height=68);hint.pack(side='left',padx=(0,16));hint.pack_propagate(False)
        ttk.Label(hint,text='좌클릭: 촬영·저장 · 우클릭: 선택 사진 삭제',style='Muted.TLabel',background=UI_COLORS['bg']).pack(anchor='w')
        self.delete_target=tk.StringVar(value='삭제 대상: 선택된 사진 없음')
        self.delete_target_label=StatusText(hint,self.delete_target,height=2,background=UI_COLORS['bg'])
        self.delete_target_label.pack(fill='x',pady=(4,0))
        for text,command in [('카메라 연결',app.start_camera),('카메라 해제',app.stop_camera),('목록 새로고침',self.refresh),('저장 폴더 열기',app.open_capture_folder)]:
            button=app.button(bar,text,command);button.configure(style='Shell.TButton');button.pack(side='left',padx=(0,8))
        body=ttk.Frame(self);body.grid(row=1,column=0,sticky='nsew');body.rowconfigure(0,weight=1)
        body.columnconfigure(0,weight=1,uniform='photos');body.columnconfigure(1,weight=1,uniform='photos')
        self.canvases=[]
        for col,title in enumerate(('실시간 원본','저장 사진')):
            card=app.card(body);card.grid(row=0,column=col,sticky='nsew',padx=(0,8) if col==0 else (0,0))
            card.columnconfigure(0,weight=1);card.rowconfigure(1,weight=1)
            head=ttk.Frame(card,style='Card.TFrame');head.grid(row=0,column=0,sticky='ew')
            ttk.Label(head,text=title,style='Section.TLabel').pack(side='left')
            if col:
                choice=ttk.Combobox(head,textvariable=self.variant,values=('원본','화면 표시본'),state='readonly',width=12)
                choice.pack(side='right');choice.bind('<<ComboboxSelected>>',self.load_selected)
            canvas=tk.Canvas(card,bg=UI_COLORS['viewport'],highlightthickness=0,width=320,height=240)
            canvas.grid(row=1,column=0,sticky='nsew',pady=8);self.canvases.append(canvas)
            label=StatusText(card,self.saved_status if col else self.live_status,height=2)
            label.grid(row=2,column=0,sticky='ew')
        self.live_canvas,self.saved_canvas=self.canvases
        self.live_canvas.bind('<Configure>',lambda e:self.paint(self.live_canvas,self.live_image))
        self.saved_canvas.bind('<Configure>',lambda e:self.paint(self.saved_canvas,self.saved_image))
        listing=ttk.Frame(self);listing.grid(row=2,column=0,sticky='ew',pady=(10,0));listing.columnconfigure(0,weight=1)
        ttk.Label(listing,textvariable=self.count,style='Section.TLabel',background=UI_COLORS['bg']).grid(row=0,column=0,sticky='w',pady=(0,5))
        self.table=ttk.Treeview(listing,columns=('file','kind'),show='headings',height=4,selectmode='browse')
        self.table.heading('file',text='촬영 파일 · 최신순');self.table.heading('kind',text='종류')
        self.table.column('file',width=650);self.table.column('kind',width=160,stretch=False)
        self.table.grid(row=1,column=0,sticky='ew');scroll=AutoScrollbar(listing,orient='vertical',command=self.table.yview)
        scroll.grid(row=1,column=1,sticky='ns');self.table.configure(yscrollcommand=scroll.set)
        self.table.bind('<<TreeviewSelect>>',self.select_photo)

    def capture_blocked(self,at,now=None):
        now=time.monotonic() if now is None else now
        if now<self.capture_ready_at:return '저장 완료 · 잠시 대기'
        if self.last_capture_source is self.app.camera and self.last_capture_at is not None and at<=self.last_capture_at:
            return '새 영상 수신 대기'
        return None

    def require_capture_ready(self,at):
        reason=self.capture_blocked(at)
        if reason:raise ValueError(reason+' · 중복 사진은 저장하지 않았습니다.')

    def capture_saved(self,at):
        self.last_capture_at=at;self.last_capture_source=self.app.camera;self.capture_ready_at=time.monotonic()+0.2
        self.app.camera_inspection.save_button.state(['disabled'])
        self.app.camera_capture_btn.state(['disabled'])

    def capture(self):
        obs=self.app.camera_view_observation()
        # Debounce without flashing the button or replacing the saved notice.
        now=time.monotonic()
        if obs and 0<=now-obs[2]<1 and self.capture_blocked(obs[2],now):return
        return self.app.save_camera()

    def delete_click(self,event=None):
        self.app.guard(self.delete_selected)
        return 'break'

    def delete_selected(self):
        if time.monotonic()<self.delete_ready_at:return
        name=self.selected
        if not name or name not in self.entries:raise ValueError('삭제할 사진을 목록에서 선택하세요.')
        if Path(name).name!=name or Path(name).suffix!='.png':raise ValueError('촬영 파일 이름을 확인하세요.')
        root=self.folder.resolve()
        files=[root/kind/Path(name).with_suffix(suffix) for kind,suffix in
               [('원본','.png'),('화면 표시본','.png'),('판정 기록','.json')]]
        # Never follow a replaced capture subfolder into unrelated user data.
        if any(p.parent.resolve()!=p.parent for p in files):raise ValueError('촬영 저장 폴더 경로가 변경되어 삭제할 수 없습니다.')
        self.delete_ready_at=time.monotonic()+0.2
        try:
            for path in files:path.unlink(missing_ok=True)
        finally:
            self.refresh(preferred=name)
            self.update_frame(time.monotonic())
        if Path(self.app.camera_save_path.get()).name==name:self.app.camera_save_path.set(str(root))
        self.app.notice('선택 사진 삭제됨 · 원본·화면 표시본·판정 기록 · '+name)

    def visible(self):
        return self.app.page=='camera' and self.app.camera_tabs.select()==str(self)

    def paint(self,canvas,image):
        if image is not None:self.app.fit_image(canvas,image,resample=Image.Resampling.BILINEAR)

    def refresh(self,preferred=None):
        try:
            paths={p.name:p for kind in ('화면 표시본','원본') for p in (self.folder/kind).glob('*.png')}
            ordered=sorted(paths,reverse=True,key=lambda n:(paths[n].stat().st_mtime_ns,n))
        except OSError as exc:
            self.saved_status.set('사진 목록을 읽을 수 없습니다: '+str(exc));return
        new=set(paths)-set(self.entries);self.entries=paths;self.last_scan=time.monotonic()
        if tuple(self.table.get_children())!=tuple(ordered):
            self.table.delete(*self.table.get_children())
            for name in ordered:self.table.insert('','end',iid=name,values=(name,'부품·조립 검사' if name.startswith('inspection_') else '상단 카메라'))
        set_text(self.count,f'촬영 목록 · {len(paths)}장 · 새 사진 우선 표시')
        chosen=preferred if preferred in paths else next((n for n in ordered if n in new),None)
        chosen=chosen or (self.selected if self.selected in paths else next(iter(ordered),None))
        if chosen:
            changed=chosen!=self.selected;self.selected=chosen
            if self.table.selection()!=(chosen,):self.table.selection_set(chosen)
            if changed or preferred:self.table.see(chosen);self.load_selected()
        else:
            self.selected=None;self.saved_image=None;self.saved_canvas.delete('image');self.saved_status.set('저장된 사진이 없습니다. 실시간 영상을 확인한 뒤 촬영하세요.')
            set_text(self.delete_target,'삭제 대상: 선택된 사진 없음')

    def select_photo(self,event=None):
        selection=self.table.selection()
        if selection and selection[0]!=self.selected:self.selected=selection[0];self.load_selected()

    def load_selected(self,event=None):
        if not self.selected:return
        set_text(self.delete_target,'삭제 대상: '+self.selected+' · 원본·표시본·판정 기록')
        path=self.folder/self.variant.get()/self.selected
        try:
            with Image.open(path) as image:self.saved_image=image.convert('RGB')
            self.saved_status.set(f'{self.selected} · {self.variant.get()} · {self.saved_image.width}×{self.saved_image.height}')
            self.paint(self.saved_canvas,self.saved_image)
        except (OSError,ValueError):
            self.saved_image=None;self.saved_canvas.delete('image');self.saved_status.set('사진을 읽을 수 없습니다 · '+str(path))

    def update_frame(self,now):
        if now-self.last_scan>=1:self.refresh()
        obs=self.app.camera_view_observation()
        now=max(now,time.monotonic())
        fresh=bool(obs and 0<=now-obs[2]<1)
        # The same button deletes the selected photo, even with no camera.
        disabled=not fresh and not self.selected
        if self.save_button.instate(['disabled'])!=disabled:self.save_button.state(['disabled'] if disabled else ['!disabled'])
        if not fresh:
            if self.live_image is not None:self.live_canvas.delete('image')
            self.live_image=None;self.live_key=None
            set_text(self.live_status,'카메라 꺼짐 또는 최신 영상 대기 · 저장 사진은 계속 볼 수 있습니다.');return
        frame,_,at=obs
        if self.live_key!=(id(frame),at):
            self.live_key=(id(frame),at);self.live_image=Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
            self.paint(self.live_canvas,self.live_image)
        set_text(self.live_status,f'실시간 원본 · {frame.shape[1]}×{frame.shape[0]} · 촬영하면 원본·표시본을 함께 저장합니다.')
