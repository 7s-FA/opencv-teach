import json,time,unittest
from pathlib import Path
from copy import deepcopy
from PIL import ImageGrab
import cv2
from . import test_ui as fixtures
from so101_teach.devices import CameraSession
from so101_teach.vision_service import MultiDetector

class StationaryDetectionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_delayed_detection_labels_and_live_video_in_both_camera_views(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-stationary';cfg=json.loads((folder/'fixture.json').read_text())
        frame=cv2.imread(str(folder/'pallet-gap.png'));a.profile=cfg['profile'];a.catalog.items=cfg['jigs']
        a.catalog.mesh=lambda key:{**deepcopy(cfg['jigs'][key]['mesh']),'shape':cfg['jigs'][key]['shape'],'method':cfg['jigs'][key]['method']}
        a.catalog.revision+=1;data=MultiDetector(a.catalog,a.profile).process(frame)
        a.camera=CameraSession(0);a.camera.running=True;now=time.monotonic()
        a.camera.observation=(frame,data,now-.8);a.camera.preview_frame=(frame,now)
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]])
        a.show_page('camera');a.camera_jig_choice.current(0);a.select_camera_jig();self.root.after_cancel(a.job)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update();now=time.monotonic()
            a.camera.observation=(frame,data,now-.8);a.camera.preview_frame=(frame,now);a.poll();self.root.after_cancel(a.job);a.draw_camera_frame(time.monotonic());self.root.update()
            for key in a.catalog.items:self.assertEqual(a.camera_jig_status.item(key,'values')[1],'0.8초 전 감지')
            self.assertIn('2/2',a.camera_overview_title.get());self.assertIn('검출 결과 0.8초 전',a.camera_status.get())
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/stationary-detection-{width}x{height}.png')
        a.camera.running=False
