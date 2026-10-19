"""Opt-in dual runtime test against an existing camera, using read-only HTTP only."""
import os
import unittest
from tests import test_ui

@unittest.skipUnless(os.environ.get('SO101_LIVE_YOLO_DUAL')=='1','Explicit dual runtime camera verification')
class DualYoloLiveTests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown

    def test_live_models_thresholds_fps_and_capture(self):
        import base64,json,shutil,socket,subprocess,threading,time
        from pathlib import Path
        from types import SimpleNamespace
        from urllib.request import Request,urlopen
        import cv2,numpy as np
        from PIL import ImageGrab
        from so101_teach.domain import ROOT
        from so101_teach.pi_connection import load,validate
        from so101_teach.remote_client import ssh_args
        from unittest.mock import Mock
        out=ROOT/'verification/yolo-dual-runtime-20261018';out.mkdir(exist_ok=True)
        c=validate(load(ROOT/'data'),True);ssh=ssh_args(c)+['-l',c['user'],c['host']]
        # Read the existing camera without a lease, camera_start, mode, or motor RPC.
        token=subprocess.check_output(ssh+['cat /home/ubuntu/OpenCV_teach/data/remote-token'],text=True).strip()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        tunnel=subprocess.Popen(ssh_args(c)+['-N','-L',f'127.0.0.1:{port}:127.0.0.1:8765','-l',c['user'],c['host']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        stopped=threading.Event();errors=[];ui_errors=[];calls=[];frames=set()
        cam=SimpleNamespace(running=True,error=None,observer=True,observation=None,preview_observation=None,
                            set_mode=Mock(),close=stopped.set,join=lambda timeout:True)
        def poll_camera():
            after=None
            while not stopped.is_set():
                try:
                    req=Request(f'http://127.0.0.1:{port}/camera',data=json.dumps({'after_preview_at':after}).encode(),
                                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                    with urlopen(req,timeout=2) as response:envelope=json.load(response)
                    if not envelope['ok']:raise RuntimeError(envelope.get('error'))
                    row=envelope['value'];calls.append('/camera')
                    encoded=row.get('preview_image') or row.get('image')
                    at=row.get('preview_at',row.get('at'))
                    if encoded and at!=after:
                        image=cv2.imdecode(np.frombuffer(base64.b64decode(encoded),np.uint8),cv2.IMREAD_COLOR)
                        cam.preview_observation=(image,{},time.monotonic()-(row['server_now']-at));after=at;frames.add(at)
                    cam.error=row.get('error')
                except Exception as exc:
                    if cam.preview_observation:errors.append(str(exc))
                stopped.wait(.06)
        thread=threading.Thread(target=poll_camera,daemon=True);thread.start()
        a=self.app;p=a.camera_yolo;a.camera=cam;a.camera_manual_off=False;a.camera_sleeping=False
        shutil.copy2(ROOT/'data/pi-connection.json',a.data_dir/'pi-connection.json')
        self.root.report_callback_exception=lambda typ,value,tb:ui_errors.append(str(value))
        a.show_page('camera');a.camera_tabs.select(p);self.root.update()
        def wait(predicate,seconds=75):
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                self.root.update();time.sleep(.01)
                if p.last_error:raise AssertionError(p.last_error)
                if predicate():return
            raise AssertionError('Timed out: '+p.status.get())
        def screenshot(name):
            self.root.update_idletasks()
            ImageGrab.grab(bbox=(self.root.winfo_rootx(),self.root.winfo_rooty(),self.root.winfo_rootx()+1920,self.root.winfo_rooty()+1043)).save(out/name)
        report={}
        try:
            wait(lambda:cam.preview_observation is not None,12)
            for key,choice in [('pc','PC · YOLO26x'),('pi','Pi · YOLO26s')]:
                p.model_choice.set(choice);p.model_changed();p.start()
                rows={};deadline=time.monotonic()+90;gaps=[];last=time.monotonic()
                while len(rows)<6 and time.monotonic()<deadline:
                    self.root.update();now=time.monotonic();gaps.append(now-last);last=now;time.sleep(.01)
                    if p.last_error:raise AssertionError(p.last_error)
                    if p.result:rows[p.result['frame_at']]={k:v for k,v in p.result.items() if k not in ('image','raw','model')}
                self.assertGreaterEqual(len(rows),6,p.status.get());self.assertIsNotNone(p.fps)
                self.assertTrue(all(r['confidence']==.5 for r in rows.values()))
                screenshot(key+'-live.png');saved=p.save_capture()
                for path in saved.parent.glob(saved.stem.replace('_인식','')+'*'):shutil.copy2(path,out/(key+'-'+path.name))
                report[key]={'frames':len(rows),'fps':p.fps,'median_inference_ms':float(np.median([r['inference_ms'] for r in rows.values()])),
                             'median_processing_ms':float(np.median([r['total_ms'] for r in rows.values()])),
                             'median_roundtrip_ms':float(np.median([r['roundtrip_ms'] for r in rows.values()])),
                             'ui_gap_p95_ms':float(np.percentile(gaps,95))*1000,'results':list(rows.values())}
                p.confidence.set('0.9');p.threshold_changed();self.assertIsNone(p.result)
                wait(lambda:p.result is not None)
                self.assertTrue(all(d['confidence']>=.9 for d in p.result['detections']))
                screenshot(key+'-confidence-09.png')
                p.stop();p.confidence.set('0.5')
            p.start();wait(lambda:p.result is not None)
            worker=p.worker.process;a.camera_tabs.select(0);self.root.update()
            self.assertFalse(p.active);self.assertIsNone(p.result);self.assertIsNotNone(worker.poll())
            a.camera_tabs.select(p);self.root.update();p.start();wait(lambda:p.result is not None)
            stopped.set();thread.join(3);cam.error='검증용 연결 종료';wait(lambda:p.result is None,3);self.assertTrue(p.save_button.instate(['disabled']))
            screenshot('camera-error-clears-results.png');p.stop()
            self.assertGreater(len(frames),8);self.assertEqual(ui_errors,[]);self.assertNotIn('상태 갱신 오류',a.message.get());self.assertIsNone(a.session);self.assertIsNone(a.leader_session)
            report.update(passed=True,distinct_camera_frames=len(frames),readonly_camera_requests=len(calls),camera_errors=errors,ui_errors=ui_errors,
                          motion_rpc_count=0,thresholds='worker smoke verifies .5 .6 .7 .8 .9',note='FPS includes existing shared camera feed; observer does not change capture rate.')
            (out/'live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({k:{field:value for field,value in row.items() if field!='results'} if isinstance(row,dict) else row for k,row in report.items()},ensure_ascii=False))
        finally:
            p.stop();stopped.set();thread.join(3);cam.running=False
            tunnel.terminate();tunnel.wait(3)
