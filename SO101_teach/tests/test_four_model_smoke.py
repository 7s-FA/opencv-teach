"""Opt-in native panel verification with a saved frame and real inference workers."""
import json,os,shutil,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_ui

@unittest.skipUnless(os.environ.get('SO101_FOUR_MODEL_SMOKE')=='1','Explicit saved-frame model verification')
class FourModelSmokeTests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown

    def test_model_selection_and_native_render(self):
        import cv2
        from PIL import ImageGrab
        from so101_teach.domain import ROOT
        from so101_teach.yolo_ui import MODEL_CHOICES
        output=ROOT/'verification/four-models-20261018';output.mkdir(exist_ok=True)
        image=cv2.imread(str(ROOT/'verification/yolo-fp16-integration-20261016/yolo_2026-10-16_21-10-50_808201_원본.png'))
        self.assertIsNotNone(image)
        a=self.app;p=a.camera_yolo
        shutil.copy2(ROOT/'data/pi-connection.json',a.data_dir/'pi-connection.json')
        cam=SimpleNamespace(running=True,error=None,observer=True,observation=None,preview_observation=None,
                            set_mode=Mock(),close=lambda:None,join=lambda timeout:True)
        a.camera=cam;a.camera_manual_off=False;a.camera_sleeping=False
        a.show_page('camera');a.camera_tabs.select(p)
        errors=[];self.root.report_callback_exception=lambda typ,value,tb:errors.append(str(value))
        selected=os.environ.get('SO101_MODEL_KEYS','pc,sam3,pi,pi_n').split(',')
        report={'input':'saved frame, simulated fresh camera timestamps; no camera or motor RPC','models':{}}
        def tick():
            cam.preview_observation=(image,{},time.monotonic());self.root.update();time.sleep(.015)
        def screenshot(name):
            self.root.update_idletasks()
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+1920,y+1043)).save(output/name)
        try:
            for label,key in MODEL_CHOICES.items():
                p.model_choice.set(label);p.model_changed();tick()
                screenshot(key+'-selection.png')
                if key not in selected:continue
                p.start();deadline=time.monotonic()+80
                while p.result is None and not p.last_error and time.monotonic()<deadline:tick()
                self.assertIsNone(p.last_error,p.last_error);self.assertIsNotNone(p.result,p.status.get())
                self.assertEqual(p.result['image'].shape,image.shape)
                self.assertTrue(all(d['confidence']>=.5 for d in p.result['detections']))
                screenshot(key+'-inference.png')
                report['models'][key]={k:v for k,v in p.result.items() if k not in ('image','raw')}
                p.stop()
            self.assertEqual(errors,[]);self.assertIsNone(a.session);self.assertIsNone(a.leader_session)
            report['passed']=True;report['ui_errors']=errors
            (output/('smoke-'+ '-'.join(selected)+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        finally:p.stop()
