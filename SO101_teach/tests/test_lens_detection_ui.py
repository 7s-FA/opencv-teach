from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import json,time,unittest
from pathlib import Path
import cv2
from tests import test_ui as fixtures
from so101_teach.vision_service import MultiDetector

class LensDetectionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_corrected_outline_and_raw_roi_render_together(self):
        folder=Path(__file__).parent/'fixtures/vision-lens';config=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'input.jpg'))
        a=self.app;a.profile=deepcopy(config['profile']);a.catalog.items=deepcopy(config['jigs']);a.catalog.revision+=1
        a.detector=MultiDetector(a.catalog,a.profile);base=time.monotonic()-3.1
        for dt in (0,.8,1.6,2.4,3.05):
            with patch('so101_teach.vision_service.time.monotonic',return_value=base+dt):result=a.detector.process(frame)
        self.assertTrue(all(r['selected'] is not None for r in result['by_jig'].values()))
        a.show_page('camera');a.active_jig='pallet';a.roi=config['jigs']['pallet']['roi'];now=time.monotonic()
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,result,now),close=lambda:None,join=lambda n:True)
        self.root.update();a.draw_camera_frame(now);self.root.update()
        shown=a.camera_display_snapshot['result']['selected'];self.assertEqual(len(shown['outline_px']),96)
        self.assertEqual(a.roi,config['jigs']['pallet']['roi'])
        from PIL import ImageGrab
        ImageGrab.grab().save('/tmp/lens-detection-ui.png')
