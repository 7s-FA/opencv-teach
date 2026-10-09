import unittest,time,json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import cv2
from . import test_ui as fixtures
from so101_teach.vision_service import MultiDetector

class PositionDetectionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_current_scene_partial_label_and_overview_at_1280_by_800(self):
        from PIL import ImageGrab
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-position';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'input.jpg'))
        a.profile=cfg['profile'];a.catalog.items={key:{**j,'stl':None,'manual_size_mm':j['mesh']['size_mm'][:2],'manual_rim_mm':j['mesh']['rim_z_mm']} for key,j in cfg['jigs'].items()}
        a.catalog.mesh=lambda key:deepcopy(cfg['jigs'][key]['mesh']);a.catalog.revision+=1;a.detector=MultiDetector(a.catalog,a.profile);data=a.detector.process(frame)
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]]);a.show_page('camera');a.camera_jig_choice.current(0);a.select_camera_jig();self.root.update();a.draw_camera_frame(time.monotonic())
        key=list(a.catalog.items)[1];self.assertEqual(a.camera_jig_status.item(key,'values')[1],'부분 감지');self.assertIn('2/2',a.camera_overview_title.get())
        def shot(name):
            self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/position-'+name+'.png')
        shot('overview');a.camera_jig_choice.current(2);a.select_camera_jig();self.root.after_cancel(a.job);a.poll();a.draw_camera_frame(time.monotonic());shot('partial')
        self.assertIn('일부가 화면 밖',a.camera_status.get());self.assertLessEqual(a.camera_status_label.winfo_rooty()+a.camera_status_label.winfo_height(),self.root.winfo_rooty()+800)
