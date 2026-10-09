import time,json,unittest
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
from so101_teach.camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
import cv2
from PIL import ImageGrab
from . import test_ui as fixtures
from so101_teach.domain import ROOT
from so101_teach.vision_service import MultiDetector

class AdoptionLatencyUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_slow_frame_keeps_adoption_label_and_outline_but_not_move_reference(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-assembly';frame=cv2.imread(str(folder/'current.png'));cfg=json.loads((folder/'fixture.json').read_text())
        a.profile=cfg['profile'];a.catalog.items=deepcopy(cfg['jigs']);a.catalog.revision+=1
        for item in a.catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        data=MultiDetector(a.catalog,a.profile).process(frame);data['by_jig']=deepcopy(data['live_by_jig'])
        now=time.monotonic()
        for r in data['by_jig'].values():r.update(pose_measured_at=now-2,pose_held=True,hold_remaining_s=8.)
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,now-(MEASUREMENT_MAX_AGE_SECONDS+.2)),preview_observation=(frame,{**data,'_preview':True},now),close=lambda:None,join=lambda n:True)
        a.camera_sleeping=False;a.camera_view_requested=True;a.show_adoption.set(True);a.refresh_adoption_display();a.camera_all.set(True);a.show_page('camera');a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]]);a.camera_jig_choice.current(0);a.select_camera_jig();self.root.update();self.root.after_cancel(a.job)
        self.assertEqual(a.camera_results(now),{})
        self.assertTrue(all(v.startswith('채택됨') for v in a.adoption_labels().values()))
        self.assertEqual(len(a.camera_adopted_positions(now)),2)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update();a.poll();self.root.after_cancel(a.job);self.root.update()
            for key in a.catalog.items:self.assertIn('채택',a.camera_jig_status.item(key,'values')[2])
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/adoption-latency-{width}x{height}.png')
