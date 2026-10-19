"""Independent live YOLO view. Results never become jig or motion decisions."""
from datetime import datetime
from collections import deque
from pathlib import Path
import json
import time
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image

from .domain import ROOT,atomic_json
from .ui_status import StatusText,set_text
from .ui_theme import COLORS
from .yolo_worker import YoloWorker,confidence_value

MODEL_PATH=ROOT/'assets/models/yolo26s_mixed_fp16'
MODEL_PATHS={'pc':ROOT/'assets/models/yolo26x_mixed_trt_fp16','sam3':ROOT/'assets/models/sam3_mixed_bf16',
             'pi':MODEL_PATH,'pi_n':ROOT/'assets/models/yolo26n_mixed_fp16'}
MODEL_CHOICES={'PC · YOLO26x':'pc','PC · SAM3':'sam3','Pi · YOLO26s':'pi','Pi · YOLO26n':'pi_n'}
MODEL_NAMES={'pc':'YOLO26x-seg','sam3':'SAM3','pi':'YOLO26s-seg','pi_n':'YOLO26n-seg'}
MAX_RESULT_AGE=5.


class YoloPanel(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,style='Surface.TFrame',padding=16)
        self.app=app;self.worker=YoloWorker();self.active=False;self.job=None;self.closed=False
        self.result=None;self.last_submitted=None;self.saved_at=None;self.source=None;self.valid_after=0.
        self.paint_key=None;self.last_error=None
        self.completed_times=deque(maxlen=30);self.fps=None;self.execution_device=None
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        bar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=0,column=0,columnspan=2,sticky='ew')
        bar.columnconfigure(6,weight=1)
        self.start_button=app.button(bar,'인식 시작',self.start);self.start_button.grid(row=0,column=0,padx=(0,8))
        self.stop_button=app.button(bar,'인식 중지',self.stop);self.stop_button.grid(row=0,column=1,padx=(0,16));self.stop_button.state(['disabled'])
        self.model_choice=tk.StringVar(value='PC · YOLO26x')
        self.model_selector=ttk.Combobox(bar,textvariable=self.model_choice,values=tuple(MODEL_CHOICES),state='readonly',width=16)
        self.model_selector.grid(row=0,column=2,padx=(0,16));self.model_selector.bind('<<ComboboxSelected>>',self.model_changed)
        ttk.Label(bar,text='최소 신뢰도').grid(row=0,column=3,padx=(0,6))
        self.confidence=tk.StringVar(value='0.5')
        self.threshold=ttk.Combobox(bar,textvariable=self.confidence,values=('0.5','0.6','0.7','0.8','0.9'),state='readonly',width=5)
        self.threshold.grid(row=0,column=4);self.threshold.bind('<<ComboboxSelected>>',self.threshold_changed)
        app.button(bar,'4분할 사진 비교',self.open_comparison).grid(row=0,column=5,padx=(16,0))
        self.view=tk.StringVar(value='인식 결과')
        choice=ttk.Combobox(bar,textvariable=self.view,values=('인식 결과','실시간 원본'),state='readonly',width=12)
        choice.grid(row=0,column=7,padx=8);choice.bind('<<ComboboxSelected>>',lambda e:self.repaint())
        app.button(bar,'카메라 연결',app.start_camera).grid(row=0,column=8,padx=(8,0))
        app.button(bar,'카메라 해제',app.stop_camera).grid(row=0,column=9,padx=(8,0))
        self.status=tk.StringVar(value='인식 대기 · 카메라를 연결한 뒤 인식 시작을 누르세요.')
        StatusText(self,self.status,height=2).grid(row=1,column=0,columnspan=2,sticky='ew',pady=8)
        self.canvas=tk.Canvas(self,bg=COLORS['nav'],highlightthickness=0)
        self.canvas.grid(row=2,column=0,sticky='nsew');self.canvas.bind('<Configure>',lambda e:self.repaint())
        summary=ttk.Frame(self,style='Card.TFrame',padding=12,width=390)
        summary.grid(row=2,column=1,sticky='nsew',padx=(12,0));summary.grid_propagate(False)
        summary.columnconfigure(0,weight=1);summary.rowconfigure(3,weight=1)
        self.model_title=tk.StringVar();self.model_detail=tk.StringVar();self.update_model_label()
        ttk.Label(summary,textvariable=self.model_title,style='Section.TLabel').grid(row=0,column=0,sticky='w')
        ttk.Label(summary,textvariable=self.model_detail,style='Small.TLabel',wraplength=350).grid(row=1,column=0,sticky='w',pady=(8,12))
        self.count=tk.StringVar(value='인식된 객체 0개')
        ttk.Label(summary,textvariable=self.count,style='Section.TLabel').grid(row=2,column=0,sticky='w',pady=(0,8))
        self.table=ttk.Treeview(summary,columns=('part','confidence'),show='headings',height=13,selectmode='none')
        self.table.heading('part',text='부품·지그');self.table.heading('confidence',text='신뢰도')
        self.table.column('part',width=245,minwidth=100);self.table.column('confidence',width=85,minwidth=65,stretch=False,anchor='e')
        from .ui_scroll import AutoScrollbar
        scroll=AutoScrollbar(summary,orient='vertical',command=self.table.yview);self.table.configure(yscrollcommand=scroll.set)
        self.table.grid(row=3,column=0,sticky='nsew');scroll.grid(row=3,column=1,sticky='ns')
        self.metrics=tk.StringVar(value='인식 FPS —\n모델 추론 — · 처리 —\n인식 결과 시각 —')
        StatusText(summary,self.metrics,height=4).grid(row=4,column=0,sticky='ew',pady=(12,8))
        self.save_button=app.button(summary,'인식 결과 저장',self.save_capture)
        self.save_button.grid(row=5,column=0,sticky='ew');self.save_button.state(['disabled'])
        app.button(summary,'저장 폴더 열기',app.open_capture_folder).grid(row=6,column=0,sticky='ew',pady=(8,0))
        ttk.Label(summary,text='마스크: 보이는 물체 영역\n신뢰도: 모델 예측 점수\n\nOpenCV 지그 검출과 독립 실행\n좌표 채택·안착 판정·모터 실행에 사용하지 않습니다.',style='Small.TLabel',wraplength=350,justify='left').grid(row=7,column=0,sticky='ew',pady=(16,0))
        self.caption=tk.StringVar(value='인식 결과는 해당 촬영 프레임에 표시됩니다.')
        ttk.Label(self,textvariable=self.caption,style='Small.TLabel').grid(row=3,column=0,columnspan=2,sticky='ew',pady=(8,0))
        self.bind('<Destroy>',lambda e:self.close() if e.widget is self else None)
        self.job=self.after(100,self.poll)

    def visible(self):
        return (getattr(self.app,'workspace_visible',True) and self.app.page=='camera'
                and self.app.camera_tabs.select()==str(self))

    def open_comparison(self):
        path=ROOT/'data/model-comparison/index.json'
        if not path.exists():raise ValueError('저장 사진 비교 결과를 준비 중입니다.')
        self.stop('저장 사진 비교 · 실시간 인식 중지')
        window=tk.Toplevel(self);window.title('SO-101 · 네 모델 사진 비교')
        window.geometry('1920x1043');window.minsize(1000,700)
        ComparisonPanel(window,path).pack(fill='both',expand=True)
        from .ui import show_ready_window
        show_ready_window(window)

    def model_key(self):
        try:return MODEL_CHOICES[self.model_choice.get()]
        except KeyError:raise ValueError('실행할 모델을 선택하세요.') from None

    def update_model_label(self):
        key=self.model_key();pc=key in ('pc','sam3')
        set_text(self.model_title,'혼합 학습 · '+MODEL_NAMES[key])
        detail=('BF16 · 컴파일 + 텍스트 캐시 · 입력 1008' if key=='sam3' else
                'FP16 · TensorRT · 입력 960' if pc else 'FP16 · NCNN · 입력 960 · 4스레드')
        detail+='\n'+('PC NVIDIA GPU · 카메라 갱신에 맞춰 최대 10 FPS' if pc else 'Pi CPU에서 추론')
        if not (MODEL_PATHS[key]/'manifest.json').is_file():detail+='\n학습·변환 완료 후 사용 가능'
        set_text(self.model_detail,detail)

    def runtime_options(self):
        path=self.app.data_dir/'yolo-runtime.json'
        if not path.exists():path=ROOT/'data/yolo-runtime.json'
        if not path.exists():raise ValueError('AI 인식 실행 환경 설정이 없습니다. tools/setup_yolo_runtime.py로 준비하세요.')
        config=json.loads(path.read_text());key=self.model_key()
        if not (MODEL_PATHS[key]/'manifest.json').is_file():
            raise ValueError(MODEL_NAMES[key]+' 모델을 준비 중입니다. 학습·변환·Pi 배포 완료 후 다시 시작하세요.')
        manifest=json.loads((MODEL_PATHS[key]/'manifest.json').read_text())
        if key in ('pc','sam3'):
            python=config['sam3_python' if key=='sam3' else 'pc_python']
            if not Path(python).is_file():raise ValueError('선택한 PC 모델의 실행 환경을 찾지 못했습니다.')
            return MODEL_PATHS[key],{'backend':'sam3' if key=='sam3' else 'tensorrt','python':python,'expected_manifest':manifest}
        from .pi_connection import load,validate
        connection=validate(load(self.app.data_dir),True)
        return config['pi_n_model' if key=='pi_n' else 'pi_model'],{'backend':'ncnn','python':config['pi_python'],'remote':connection,
                                   'remote_dir':config['pi_app_dir'],'expected_manifest':manifest}

    def start(self):
        if self.active:return
        self.update_model_label()
        confidence_value(self.confidence.get());model_path,options=self.runtime_options()
        if not self.app.camera or not self.app.camera.running:self.app.start_camera()
        self.clear_result();self.valid_after=time.monotonic();self.source=self.app.camera
        self.worker.start(model_path,**options);self.active=True;self.last_error=None
        self.execution_device=('PC NVIDIA GPU · PyTorch BF16' if self.model_key()=='sam3' else
                               'PC NVIDIA GPU · TensorRT FP16' if self.model_key()=='pc' else 'Pi CPU · NCNN FP16')
        self.start_button.state(['disabled']);self.stop_button.state(['!disabled'])
        set_text(self.status,self.execution_device+' 모델 불러오는 중 · 카메라 연결 유지')
        self.app.update_camera_mode()

    def stop(self,message='인식 중지 · 다시 시작하면 새 영상으로 인식합니다.'):
        self.active=False;self.worker.close();self.clear_result();self.last_submitted=None
        self.start_button.state(['!disabled']);self.stop_button.state(['disabled'])
        set_text(self.status,message)

    def clear_result(self):
        self.result=None;self.paint_key=None;self.saved_at=None
        self.completed_times.clear();self.fps=None
        self.table.delete(*self.table.get_children());set_text(self.count,'인식된 객체 0개')
        set_text(self.metrics,'인식 FPS —\n모델 추론 — · 처리 —\n인식 결과 시각 —');self.save_button.state(['disabled'])
        self.canvas.delete('image');self.canvas.delete('empty')

    def threshold_changed(self,event=None):
        was_active=self.active
        self.stop('신뢰도 변경 · 새 영상 인식 대기')
        if was_active:self.app.guard(self.start)

    def model_changed(self,event=None):
        was_active=self.active;self.stop('모델 변경 · 새 영상 인식 대기');self.update_model_label()
        if was_active:self.app.guard(self.start)

    def repaint(self):
        self.paint_key=None

    def poll(self):
        if self.closed:return
        try:
            if not self.visible():
                if self.active:self.stop('탭 전환으로 인식 중지 · 인식 시작을 눌러 다시 실행하세요.')
            else:self.update_frame(time.monotonic())
        except Exception as exc:
            self.last_error=str(exc);self.stop('인식 오류 · '+str(exc))
        finally:
            if not self.closed:self.job=self.after(30 if self.active else 100,self.poll)

    def update_frame(self,now):
        if self.active and self.source is None and self.app.camera is not None:self.source=self.app.camera
        if self.active and self.source is not self.app.camera:
            self.stop('카메라가 변경되었습니다. 인식 시작을 눌러 다시 실행하세요.')
        obs=self.app.camera_view_observation()
        now=max(now,time.monotonic())
        fresh=bool(obs and 0<=now-obs[2]<1 and not getattr(self.app.camera,'error',None)
                   and not self.app.camera_sleeping and not self.app.camera_manual_off)
        item=self.worker.poll() if self.active else None
        if item and item['kind']=='error':
            self.last_error=item['error'];self.stop('인식 오류 · '+item['error']);return
        if not fresh:
            self.valid_after=now
            if self.result is not None:self.clear_result()
            self.canvas.delete('image');self.canvas.delete('empty')
            self.canvas.create_text(max(1,self.canvas.winfo_width())/2,max(1,self.canvas.winfo_height())/2,
                                    text='카메라 영상 대기',fill='white',font=('Noto Sans CJK KR',16),tags='empty')
            if self.active:set_text(self.status,'최신 카메라 영상 대기 · 이전 인식 결과는 표시하지 않습니다.')
            self.save_button.state(['disabled']);return
        frame,_,frame_at=obs
        if item and item['kind']=='result' and self.valid_after<=item['frame_at']<=now and now-item['frame_at']<=MAX_RESULT_AGE:
            self.result=item
            self.completed_times.append(item.get('finished_at',now))
            while len(self.completed_times)>2 and self.completed_times[-1]-self.completed_times[0]>5:self.completed_times.popleft()
            if len(self.completed_times)>1:
                duration=self.completed_times[-1]-self.completed_times[0]
                self.fps=(len(self.completed_times)-1)/duration if duration>0 else None
            self.table.delete(*self.table.get_children())
            for row in item['detections']:
                self.table.insert('','end',values=(row['label'],f"{row['confidence']*100:.1f}%"))
            set_text(self.count,f"인식된 객체 {len(item['detections'])}개")
        if self.active and frame_at!=self.last_submitted and self.worker.submit(frame,frame_at,float(self.confidence.get())):
            self.last_submitted=frame_at
        result=self.result
        if result and now-result['frame_at']>MAX_RESULT_AGE:
            self.clear_result();result=None
        show_result=self.view.get()=='인식 결과' and result is not None
        shown=result['image'] if show_result else frame
        stamp=result['frame_at'] if show_result else frame_at
        key=(stamp,show_result,self.canvas.winfo_width(),self.canvas.winfo_height())
        if key!=self.paint_key:
            self.canvas.delete('empty');self.app.fit_image(self.canvas,Image.fromarray(cv2.cvtColor(shown,cv2.COLOR_BGR2RGB)));self.paint_key=key
        if self.active:
            message='인식 중 · 마스크와 신뢰도는 표시된 촬영 프레임의 결과입니다.' if result else '첫 영상 인식 중…' if self.worker.ready else '인식 모델 불러오는 중…'
            set_text(self.status,message)
        if result:
            age=max(0.,now-result['frame_at'])
            fps=f'{self.fps:.1f}' if self.fps is not None else '측정 중'
            set_text(self.metrics,f"인식 FPS {fps}\n모델 추론 {result['inference_ms']:.1f}ms · 처리 {result['total_ms']:.1f}ms\n결과 {age:.1f}초 전")
        enabled=bool(self.active and result and self.saved_at!=result['frame_at'])
        self.save_button.state(['!disabled'] if enabled else ['disabled'])
        set_text(self.caption,'AI 인식 결과 · 해당 프레임에만 마스크 표시' if show_result else '실시간 원본 · 인식 결과가 겹쳐지지 않은 카메라 영상')

    def save_capture(self):
        result=self.result;now=time.monotonic();obs=self.app.camera_view_observation()
        if (not self.active or not result or self.saved_at==result['frame_at'] or not 0<=now-result['frame_at']<=MAX_RESULT_AGE
                or self.source is not self.app.camera or getattr(self.app.camera,'error',None)
                or not obs or not 0<=now-obs[2]<1 or self.app.camera_sleeping or self.app.camera_manual_off):
            raise ValueError('저장할 최신 인식 결과가 없습니다.')
        folder=self.app.data_dir/'captures'/'YOLO';folder.mkdir(parents=True,exist_ok=True)
        name='yolo_'+datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
        raw=folder/(name+'_원본.png');shown=folder/(name+'_인식.png');meta=folder/(name+'.json')
        try:
            for path,image in ((raw,result['raw']),(shown,result['image'])):
                ok,data=cv2.imencode('.png',image)
                if not ok:raise OSError('YOLO 결과 영상 저장 실패')
                path.write_bytes(data.tobytes())
            atomic_json(meta,{'model':result.get('model') or json.loads((MODEL_PATHS[self.model_key()]/'manifest.json').read_text()),'execution_device':self.execution_device,
                             'runtime':result.get('runtime'),
                             'captured_wall_time':time.time()-(now-result['frame_at']),'result_age_seconds':now-result['frame_at'],
                             'confidence_threshold':result['confidence'],'inference_ms':result['inference_ms'],
                             'total_ms':result['total_ms'],'effective_fps':self.fps,'roundtrip_ms':result.get('roundtrip_ms'),'detections':result['detections'],
                             'raw_image':raw.name,'annotated_image':shown.name,'used_for_motion':False})
        except Exception:
            for path in (raw,shown,meta):path.unlink(missing_ok=True)
            raise
        self.saved_at=result['frame_at'];self.save_button.state(['disabled'])
        self.app.notice('AI 인식 결과·원본·기록 저장됨 · '+str(folder))
        return shown

    def close(self):
        if self.closed:return
        self.closed=True
        if self.job:self.after_cancel(self.job);self.job=None
        self.worker.close()


class ComparisonPanel(ttk.Frame):
    """Offline four-way view of identical photos; opening it runs no inference or devices."""
    ORDER=('pc','sam3','pi','pi_n')

    def __init__(self,parent,index_path):
        super().__init__(parent,style='Surface.TFrame',padding=12)
        self.folder=Path(index_path).parent;self.document=json.loads(Path(index_path).read_text())
        self.items=self.document['images']
        if self.document.get('status')!='complete' or not self.items:raise ValueError('완료된 사진 비교 결과가 없습니다.')
        self.position=0;self.images={};self.photos={};self.canvases={};self.details={}
        self.columnconfigure(0,weight=1);self.rowconfigure(2,weight=1)
        bar=ttk.Frame(self,style='Card.TFrame');bar.grid(row=0,column=0,sticky='ew',pady=(0,6))
        ttk.Label(bar,text='네 모델 · 같은 사진 비교',style='Section.TLabel').pack(side='left',padx=(0,24))
        ttk.Button(bar,text='◀ 이전',command=lambda:self.move(-1)).pack(side='left')
        self.choice=tk.StringVar()
        self.selector=ttk.Combobox(bar,textvariable=self.choice,state='readonly',width=21,
                                  values=[f'{i+1:02d} / {len(self.items):02d} · {row["id"]}' for i,row in enumerate(self.items)])
        self.selector.pack(side='left',padx=8);self.selector.bind('<<ComboboxSelected>>',self.select)
        ttk.Button(bar,text='다음 ▶',command=lambda:self.move(1)).pack(side='left')
        self.original=tk.BooleanVar(value=False)
        ttk.Checkbutton(bar,text='원본 보기',variable=self.original,command=self.refresh).pack(side='left',padx=20)
        ttk.Label(bar,text='사진 더블클릭: 확대  ·  ← →: 사진 전환',style='Small.TLabel').pack(side='right')
        ttk.Label(self,text='밝을 때 촬영한 실물 테스트 사진 · 최소 신뢰도 0.5 · 위: PC GPU / 아래: Pi CPU · 카메라 연결 없이 볼 수 있습니다.',
                  style='Small.TLabel').grid(row=1,column=0,sticky='w',pady=(0,8))
        grid=ttk.Frame(self,style='Surface.TFrame');grid.grid(row=2,column=0,sticky='nsew')
        for n in (0,1):grid.rowconfigure(n,weight=1,uniform='model');grid.columnconfigure(n,weight=1,uniform='model')
        for i,key in enumerate(self.ORDER):
            card=ttk.Frame(grid,style='Card.TFrame',padding=8);card.grid(row=i//2,column=i%2,sticky='nsew',padx=4,pady=4)
            card.columnconfigure(0,weight=1);card.rowconfigure(1,weight=1)
            header=ttk.Frame(card,style='Card.TFrame');header.grid(row=0,column=0,sticky='ew',pady=(0,4))
            label=('PC · ' if i<2 else 'Pi · ')+MODEL_NAMES[key]
            if key=='sam3' and self.items[0]['models'][key].get('model',{}).get('runtime_optimization'):label+=' · 최적화'
            ttk.Label(header,text=label,style='Section.TLabel').pack(side='left')
            detail=tk.StringVar();self.details[key]=detail
            ttk.Label(header,textvariable=detail,style='Small.TLabel').pack(side='right')
            canvas=tk.Canvas(card,bg=COLORS['nav'],highlightthickness=0);canvas.grid(row=1,column=0,sticky='nsew')
            canvas.bind('<Configure>',lambda event,k=key:self.paint(k))
            canvas.bind('<Double-Button-1>',lambda event,k=key:self.zoom(k));self.canvases[key]=canvas
        ttk.Label(self,text='표시 시간은 저장 사진별 1회 처리 값이며 실시간 FPS·정확도 순위를 뜻하지 않습니다. SAM3: BF16 / YOLO26x·s·n: FP16. 최적화 표시는 저장 결과에 적용된 설정입니다.',
                  style='Small.TLabel').grid(row=3,column=0,sticky='w',pady=(6,0))
        self.bind('<Left>',lambda e:self.move(-1));self.bind('<Right>',lambda e:self.move(1))
        for widget in (self.selector,*self.canvases.values()):
            widget.bind('<Left>',lambda e:self.move(-1));widget.bind('<Right>',lambda e:self.move(1))
        self.refresh();self.focus_set()

    def move(self,delta):
        self.position=(self.position+delta)%len(self.items);self.refresh();return 'break'

    def select(self,event=None):
        self.position=self.selector.current();self.refresh()

    def refresh(self):
        from PIL import Image
        row=self.items[self.position];self.selector.current(self.position)
        for key in self.ORDER:
            value=row['models'][key]
            path=row['original'] if self.original.get() else value['image']
            with Image.open(self.folder/path) as image:self.images[key]=image.copy()
            self.details[key].set(f'{len(value["detections"])}개 · 추론 {value["inference_ms"]:.0f}ms · 처리 {value["total_ms"]:.0f}ms')
            self.paint(key)

    def paint(self,key):
        if key not in self.images:return
        from PIL import ImageTk
        canvas=self.canvases[key];image=self.images[key].copy()
        image.thumbnail((max(1,canvas.winfo_width()),max(1,canvas.winfo_height())),Image.Resampling.LANCZOS)
        self.photos[key]=ImageTk.PhotoImage(image,master=self)
        canvas.delete('all');canvas.create_image(canvas.winfo_width()/2,canvas.winfo_height()/2,image=self.photos[key])

    def zoom(self,key):
        from PIL import ImageTk
        row=self.items[self.position];window=tk.Toplevel(self)
        window.title(f'{MODEL_NAMES[key]} · {row["id"]} · '+('원본' if self.original.get() else '인식 결과'))
        window.geometry('1600x950');canvas=tk.Canvas(window,bg=COLORS['nav'],highlightthickness=0);canvas.pack(fill='both',expand=True)
        source=self.images[key].copy()
        def paint(event=None):
            image=source.copy();image.thumbnail((max(1,canvas.winfo_width()),max(1,canvas.winfo_height())),Image.Resampling.LANCZOS)
            canvas.photo=ImageTk.PhotoImage(image,master=window);canvas.delete('all')
            canvas.create_image(canvas.winfo_width()/2,canvas.winfo_height()/2,image=canvas.photo)
        canvas.bind('<Configure>',paint);window.bind('<Escape>',lambda e:window.destroy())
