"""Production workspaces with isolated data and explicitly recorded camera input."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json,shutil,time,uuid
from .domain import ROOT,read_json,atomic_json
from .devices import CameraSession

SCENES={'complete':'정상 조립 영상','faults':'오안착·미완성 영상'}


def prepare_data(source=None,destination=None):
    source=Path(source or ROOT/'data').resolve()
    destination=Path(destination or ROOT/'demo_sessions'/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])).resolve()
    if destination==source or destination.is_relative_to(source):raise ValueError('데모 데이터는 실사용 데이터 밖에 저장해야 합니다.')
    destination.mkdir(parents=True,exist_ok=False)
    for name in ('profile.json','robot_profiles.json','jigs.json','workcell-preview.json','model-adjustments.json','preferences.json','pi-connection.json'):
        if (source/name).is_file():shutil.copy2(source/name,destination/name)
    for name in ('calibration','episodes','inspection_reference','pi-episodes','jig_assets','camera_images'):
        if (source/name).is_dir():shutil.copytree(source/name,destination/name)
    # References into the live data folder must also point into this copy.
    def relocate(value):
        if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
        if isinstance(value,list):return [relocate(v) for v in value]
        if isinstance(value,str) and value.startswith(str(source)+'/'):return str(destination)+value[len(str(source)):]
        return value
    for path in destination.rglob('*.json'):
        value=read_json(path);new=relocate(value)
        if new!=value:atomic_json(path,new)
    preferences=read_json(destination/'preferences.json') if (destination/'preferences.json').exists() else {}
    preferences['device_host']='local';atomic_json(destination/'preferences.json',preferences)
    atomic_json(destination/'demo-session.json',{'source':str(source),'created_at':time.time(),'device_access':False})
    return destination


class ReplayCamera(CameraSession):
    def __init__(self,app):
        super().__init__('recorded-demo',processor=app.detector.process)
        self.app=app
    def run(self):
        import cv2,threading
        try:
            frames={key:cv2.imread(str(ROOT/'tests/fixtures/inspection-all'/(key+'.jpg'))) for key in SCENES}
            if any(frame is None for frame in frames.values()):raise ValueError('데모 저장 영상을 찾을 수 없습니다.')
            self.processing_thread=threading.Thread(target=self.process_frames,name='SO101-demo-vision',daemon=True);self.processing_thread.start()
            while not self.stop.is_set():
                if getattr(self.app,'demo_video_gap',False):self.stop.wait(.05);continue
                frame=frames[getattr(self.app,'demo_scene','complete')];at=time.monotonic()
                self.frame=frame;self.frame_at=at;self.preview_frame=(frame,at)
                with self.frame_lock:
                    if self.processing_enabled:self.pending_frame=(frame,at);self.frame_ready.set()
                self.stop.wait(1/self.preview_fps)
        except Exception as exc:self.error=str(exc)
        finally:
            self.stop.set();self.frame_ready.set()
            if self.processing_thread:self.processing_thread.join(3)
            self.running=False


def activate(workspaces,scene='complete'):
    """Attach only after the same two-arm shell as production has been built."""
    cfg=read_json(ROOT/'tests/fixtures/inspection-all/fixture.json')
    for app in workspaces.apps.values():
        app.demo_scene=scene;app.demo_video_gap=False
        # Replay the recorded linear endpoint, retaining current installation,
        # calibration, episodes and arm-specific coordinate transforms.
        if app.workcell_preview and app.workcell_preview.get('linear_stage'):
            app.workcell_preview['linear_stage']['startup_state']=deepcopy(cfg['placement']['linear_stage']['startup_state'])
            app.refresh_linear_status()
        app.root.title(app.root.title()+' · 데모 · 저장 영상 재생')
        app.device_target.set('장치: 데모')
        def choose(event=None,a=app):
            a.demo_scene='faults' if a.demo_scene=='complete' else 'complete'
            a.camera_inspection.clear();a.camera_inspection.generation+=1
            if a.camera:a.camera.set_processor(a.detector.process)
            a.notice('데모 · '+SCENES[a.demo_scene]+' · F7 수신 중단/복구 · F8 화면 저장')
            return 'break'
        def gap(event=None,a=app):
            a.demo_video_gap=not a.demo_video_gap
            a.notice('데모 · '+('영상 수신 중단' if a.demo_video_gap else '영상 수신 복구'))
            return 'break'
        def capture(event=None,a=app):
            from PIL import ImageGrab
            page=a.page;deadline=time.monotonic()+10
            def save_when_ready():
                if a.closed:return
                if a.page!=page:a.notice('화면이 바뀌어 데모 캡처를 취소했습니다.');return
                ready=True
                if page in ('teach','model') and a.render_enabled:
                    ready=a.last_rgb is not None and a.pending_render_context is None
                if page=='camera' and not a.demo_video_gap and a.camera_tabs.select()==str(a.camera_inspection) and a.camera_inspection.mode.get()=='카메라 영상':
                    panel=a.camera_inspection;snapshot=panel.capture_snapshot;result=panel.worker.result
                    ready=bool(snapshot and snapshot['metadata']['inspection_valid'] and result and result['key']==panel.context
                               and result['rows'] and all(row.get('state')!='checking' for row in result['rows']))
                if not ready:
                    if time.monotonic()<deadline:a.root.after(80,lambda:a.guard(save_when_ready));return
                    a.notice('데모 화면 갱신이 완료되지 않아 저장하지 않았습니다. 다시 F8을 눌러주세요.',True);return
                folder=a.data_dir/'captures'/'앱 화면';folder.mkdir(parents=True,exist_ok=True)
                path=folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.png')
                a.root.update_idletasks();x,y=a.root.winfo_rootx(),a.root.winfo_rooty()
                ImageGrab.grab(bbox=(x,y,x+a.root.winfo_width(),y+a.root.winfo_height())).save(path)
                a.notice('데모 화면 저장: '+str(path))
            save_when_ready();return 'break'
        app.root.bind('<F6>',choose);app.root.bind('<F7>',gap);app.root.bind('<F8>',lambda event,a=app,fn=capture:a.guard(lambda:fn(event)))
        app.notice('데모 · 저장 영상 재생 · F6 정상/오안착 · F7 수신 중단/복구 · F8 화면 저장')
        app.guard(app.connect_devices)
