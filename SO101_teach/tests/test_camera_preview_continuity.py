import time
import unittest
from unittest.mock import patch
from tests import test_camera_overview as fixtures


class CameraPreviewContinuityTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_slow_detection_never_deletes_fresh_video_or_reuses_expired_roi(self):
        frame,data=self.camera();a=self.app;p=a.camera_inspection
        a.camera_tabs.select(p);p.station.set('완성품 팔레트');self.root.update()
        now=time.monotonic()
        from so101_teach.camera_inspection_ui import all_anchors
        with patch('so101_teach.camera_inspection_ui.all_anchors',wraps=all_anchors) as draw_roi:
            image_id=None
            for delta,age in [(0,.8),(.1,1.2),(.2,1.45),(.3,.7),(.4,1.1)]:
                a.camera.observation=(frame,{**data,'detection_frame_at':now+delta-age},now+delta-.03)
                before=draw_roi.call_count
                a.draw_camera_frame(now+delta,scheduled=True)
                image=p.canvas.find_withtag('image')
                self.assertEqual(len(image),1)
                if image_id is None:image_id=image[0]
                self.assertEqual(image[0],image_id)
                self.assertEqual(draw_roi.call_count-before,1)
                if age>=1:self.assertEqual(draw_roi.call_args.args[1],{})
                self.assertIn('전체 영역 자동 비교' if age<1 else '영상 정상',p.status.get())
    def test_preview_without_any_detection_stays_visible_but_expired_video_is_cleared(self):
        frame,_=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);self.root.update()
        now=time.monotonic();a.camera.observation=(frame,{'detection_frame_at':None},now)
        a.draw_camera_frame(now,scheduled=True)
        self.assertTrue(p.canvas.find_withtag('image'));self.assertIn('영상 정상',p.status.get())
        a.draw_camera_frame(now+1.01,scheduled=True)
        self.assertFalse(p.canvas.find_withtag('image'))
        self.assertIn('최신 카메라 영상 대기',p.status.get())
