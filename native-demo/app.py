"""Launch the original Tk app; only camera input is replaced with saved photos."""
import os,sys,time,threading,json
from pathlib import Path
ROOT=Path(os.environ['SO101_DEMO_APP']);sys.path.insert(0,str(ROOT))

def main():
    import tkinter as tk
    import cv2
    from so101_teach.devices import CameraSession
    import so101_teach.ui as ui
    from so101_teach.arm_workspaces import ArmWorkspaces
    from so101_teach.workcell_preview import WORKCELL_VIEW
    class PhotoCamera(CameraSession):
        def run(self):
            try:
                image=cv2.imread(str(ROOT/'tests/fixtures/inspection-all/faults.jpg'))
                if image is None:raise RuntimeError('데모 카메라 사진을 찾지 못했습니다.')
                self.processing_thread=threading.Thread(target=self.process_frames,daemon=True);self.processing_thread.start()
                current='faults'
                while not self.stop.is_set():
                    try:chosen=json.loads((ROOT.parent/'camera.json').read_text())['case']
                    except (OSError,ValueError,KeyError):chosen=current
                    if chosen!=current and chosen in ('faults','complete'):
                        image=cv2.imread(str(ROOT/f'tests/fixtures/inspection-all/{chosen}.jpg'));current=chosen
                    at=time.monotonic();frame=image.copy()
                    self.frame=frame;self.frame_at=at;self.preview_frame=(frame,at)
                    with self.frame_lock:
                        if self.processing_enabled:self.pending_frame=(frame,at);self.frame_ready.set()
                    self.stop.wait(.1)
            except Exception as exc:self.error=str(exc)
            finally:
                self.stop.set();self.frame_ready.set();self.running=False
                if self.processing_thread:self.processing_thread.join(3)
    ui.CameraSession=PhotoCamera
    root=tk.Tk();root.withdraw()
    manager=ArmWorkspaces(root,auto_camera=False,auto_devices=False,mode_override='demo')
    for app in manager.apps.values():
        app.view=WORKCELL_VIEW
        app.root.geometry('1920x1080+0+0')
        app.root.overrideredirect(True)
        app.guidance.set('원본 프로그램 · 브라우저 데모 / 카메라: 저장된 오안착 사진 / 모터: 가상 장치')
        app.connect_devices();app.set_mode('target')
        original=app.camera_page_changed
        def camera_page(previous,app=app,original=original):
            original(previous)
            if app.page=='camera':app.guard(app.start_camera)
        app.camera_page_changed=camera_page
    root.deiconify();root.geometry('1920x1080+0+0')
    def report():
        app=manager.apps[manager.active]
        state={'ready':True,'page':app.page,'rendered':app.last_rgb is not None,'camera_frame':app.camera is not None and app.camera.frame is not None,'camera_error':getattr(app.camera,'error',None),'message':app.message.get(),'renderer_alive':app.renderer.process.is_alive() if app.renderer else None,'motor_modes':[a.profile.get('mode') for a in manager.apps.values()]}
        file=ROOT.parent/'ui-state.json';temporary=file.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.replace(file)
        root.after(500,report)
    root.after(500,report)
    root.mainloop()

if __name__=='__main__':main()
