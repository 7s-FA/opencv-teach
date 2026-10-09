"""Original SO-101 Tk application with offline motor and saved-photo inputs."""
import argparse,json,os,sys,threading,time,traceback
from pathlib import Path

if not getattr(sys,'frozen',False):sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'SO101_teach'))

def main():
    parser=argparse.ArgumentParser(description='SO-101 Teach 다운로드 데모')
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--smoke-test',type=Path,help='Write verification report and exit')
    args=parser.parse_args()
    from so101_teach.domain import ROOT
    from data import default_data,prepare_data
    data=prepare_data(ROOT,args.data_dir or default_data())
    from offline import install
    install()
    import tkinter as tk
    from tkinter import ttk
    import cv2
    from so101_teach.devices import CameraSession
    import so101_teach.ui as ui
    from so101_teach.arm_workspaces import ArmWorkspaces
    from so101_teach.workcell_preview import WORKCELL_VIEW
    case={'value':'jigs'}
    photo_root=ROOT/'demo-photos' if getattr(sys,'frozen',False) else Path(__file__).resolve().parent/'photos'
    photos={'jigs':photo_root/'jigs.jpg','complete':ROOT/'tests/fixtures/inspection-all/complete.jpg','faults':ROOT/'tests/fixtures/inspection-all/faults.jpg'}
    class PhotoCamera(CameraSession):
        def run(self):
            try:
                current=None;image=None
                self.processing_thread=threading.Thread(target=self.process_frames,daemon=True);self.processing_thread.start()
                while not self.stop.is_set():
                    chosen=case['value']
                    if chosen!=current:
                        # imdecode handles Korean Windows account/directory names.
                        import numpy as np
                        image=cv2.imdecode(np.frombuffer(photos[chosen].read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR)
                        if image is None:raise RuntimeError('데모 카메라 사진을 읽을 수 없습니다.')
                        current=chosen
                    at=time.monotonic();frame=image.copy();self.frame=frame;self.frame_at=at;self.preview_frame=(frame,at)
                    with self.frame_lock:
                        if self.processing_enabled:self.pending_frame=(frame,at);self.frame_ready.set()
                    self.stop.wait(.1)
            except Exception as exc:self.error=str(exc)
            finally:
                self.stop.set();self.frame_ready.set();self.running=False
                if self.processing_thread:self.processing_thread.join(3)
    ui.CameraSession=PhotoCamera
    root=tk.Tk();root.withdraw();errors=[]
    def callback_error(kind,value,tb):
        errors.append(''.join(traceback.format_exception(kind,value,tb)))
        with (data/'runtime.log').open('a',encoding='utf-8') as f:f.write(errors[-1])
        if not args.smoke_test:
            from tkinter import messagebox
            messagebox.showerror('데모 실행 오류',str(value),parent=root)
    root.report_callback_exception=callback_error
    manager=ArmWorkspaces(root,data_dir=data,auto_camera=False,auto_devices=False,mode_override='demo')
    options={'지그 2개 검출':'jigs','조립 완료 사진':'complete','오안착 사진':'faults'}
    selectors=[]
    def change_photo(label):
        case['value']=options[label]
        for choice in selectors:choice.set(label)
        for app in manager.apps.values():
            app.detector.clear();app.camera_display_snapshot=None
        manager.apps[manager.active].notice(label+' · 저장된 사진으로 위치를 다시 측정합니다.')
    for app in manager.apps.values():
        app.view=WORKCELL_VIEW;app.root.title('SO-101 Teach · 다운로드 데모')
        width=min(1920,app.root.winfo_screenwidth());height=min(1080,app.root.winfo_screenheight()-70)
        app.root.geometry(f'{width}x{height}+0+0')
        app.guidance.set('오프라인 데모 · 사진 입력 / 가상 모터 · 편집 내용은 이 PC에 저장됩니다.')
        app.connect_devices();app.set_mode('target')
        menu=tk.Menu(app.root);photo_menu=tk.Menu(menu,tearoff=False)
        for label in options:photo_menu.add_command(label=label,command=lambda label=label:change_photo(label))
        menu.add_cascade(label='데모 사진',menu=photo_menu);app.root.config(menu=menu)
        original=app.camera_page_changed
        def camera_page(previous,app=app,original=original):
            original(previous)
            if app.page=='camera':app.guard(app.start_camera)
        app.camera_page_changed=camera_page
    ui.show_ready_window(root)
    if args.smoke_test:
        from smoke import run_smoke
        run_smoke(root,manager,args.smoke_test,errors,change_photo,case)
    root.mainloop()
    if args.smoke_test:
        report=json.loads(args.smoke_test.read_text(encoding='utf-8')) if args.smoke_test.exists() else {}
        if not report.get('passed'):raise SystemExit(1)

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    os.environ.setdefault('OPENBLAS_NUM_THREADS','1');os.environ.setdefault('OMP_NUM_THREADS','1');os.environ.setdefault('MUJOCO_GL','glfw')
    try:main()
    except Exception:
        from data import default_data
        folder=default_data();folder.mkdir(parents=True,exist_ok=True)
        error=traceback.format_exc();(folder/'startup-error.log').write_text(error,encoding='utf-8')
        if '--smoke-test' not in sys.argv:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw();messagebox.showerror('SO-101 데모 시작 실패',error+'\n기록: '+str(folder));root.destroy()
        else:
            output=Path(sys.argv[sys.argv.index('--smoke-test')+1]);output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps({'passed':False,'errors':[error]},ensure_ascii=False,indent=2),encoding='utf-8')
            if sys.stderr:print(error,file=sys.stderr)
        raise SystemExit(1)
