"""Display caching must not retain mutated pixels, expired poses or old results."""
import time,unittest
from unittest.mock import patch
import numpy as np
from tests import test_camera_overview as fixtures

class DisplayCacheTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_identical_pixels_reuse_upload_but_inplace_changes_do_not(self):
        a=self.app;c=a.camera_canvas;pixels=np.zeros((20,30,3),np.uint8)
        a.fit_image(c,pixels);photo=c.photo
        a.fit_image(c,pixels.copy());self.assertIs(c.photo,photo)
        pixels[0,0]=255;a.fit_image(c,pixels);self.assertIsNot(c.photo,photo)
        photo=c.photo;c.delete('image');a.fit_image(c,pixels);self.assertIsNot(c.photo,photo)
    def test_adoption_enabled_allows_static_frame_skip_but_expiry_repaints(self):
        frame,data=self.camera();a=self.app;a.show_adoption.set(True)
        a.camera_display_poses.clear();now=time.monotonic();a.camera.observation=(frame,data,now)
        visible={'pallet':{**data['by_jig']['pallet'],'pose_held':True}}
        with patch.object(a,'camera_adopted_positions',return_value=visible),patch.object(a,'draw_roi') as paint:
            a.draw_camera_frame(now,scheduled=True);a.camera_display_poses.clear()
            a.draw_camera_frame(now+.01,scheduled=True);self.assertEqual(paint.call_count,1)
            with patch.object(a,'camera_adopted_positions',return_value={}):a.draw_camera_frame(now+.02,scheduled=True)
            self.assertEqual(paint.call_count,2)
    def test_new_inspection_result_repaints_even_without_new_video(self):
        frame,data=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);self.root.update()
        now=time.monotonic();a.camera.observation=(frame,data,now);a.camera_display_poses.clear()
        with patch.object(p,'update_frame') as paint:
            p.worker.result=None;a.draw_camera_frame(now,scheduled=True)
            p.worker.result={'at':now,'completed':now};a.draw_camera_frame(now+.01,scheduled=True)
            self.assertEqual(paint.call_count,2)
