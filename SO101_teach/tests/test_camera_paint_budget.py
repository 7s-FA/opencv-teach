import time
from unittest.mock import patch
import unittest
from tests import test_camera_overview as fixtures


class CameraPaintBudgetTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_roi_draws_only_visible_canvas_and_static_cad_draws_neither(self):
        frame,data=self.camera();a=self.app;p=a.camera_inspection;a.camera_tabs.select(p);self.root.update()
        a.camera.observation=(frame,data,time.monotonic())
        with patch.object(a,'fit_image',wraps=a.fit_image) as paint:
            a.draw_camera_frame(time.monotonic())
            self.assertEqual([call.args[0] for call in paint.call_args_list],[p.canvas])
            paint.reset_mock();p.mode.set('CAD 설명');a.draw_camera_frame(time.monotonic(),scheduled=True)
            paint.assert_not_called()
    def test_repeated_frame_is_skipped_but_new_frame_resize_and_expiry_are_not(self):
        frame,data=self.camera();a=self.app;a.camera_tabs.select(a.camera_inspection);self.root.update()
        now=time.monotonic();a.camera.observation=(frame,data,now);a.camera_paint_key=None
        with patch.object(a.camera_inspection,'update_frame',wraps=a.camera_inspection.update_frame) as paint:
            a.draw_camera_frame(now,scheduled=True);a.draw_camera_frame(now+.03,scheduled=True)
            self.assertEqual(paint.call_count,1)
            a.camera.observation=(frame,data,now+.04);a.draw_camera_frame(now+.04,scheduled=True)
            self.assertEqual(paint.call_count,2)
            paint.reset_mock();width=a.camera_inspection.canvas.winfo_width()
            with patch.object(a.camera_inspection.canvas,'winfo_width',return_value=width+20):
                a.draw_camera_frame(now+.05,scheduled=True)
            self.assertEqual(paint.call_count,1)
            a.draw_camera_frame(now+1.1,scheduled=True)
            self.assertFalse(a.camera_inspection.canvas.find_withtag('image'))
    def test_same_frame_metadata_refresh_and_expired_detection(self):
        frame,data=self.camera();a=self.app;a.camera_tabs.select(a.camera_inspection);self.root.update()
        now=time.monotonic();data={**data,'detection_frame_at':now-.85};a.camera.observation=(frame,data,now)
        a.draw_camera_frame(now,scheduled=True)
        a.draw_camera_frame(now+.16,scheduled=True)
        self.assertTrue(a.camera_inspection.canvas.find_withtag('image'))
        self.assertIn('검출 갱신 대기',a.camera_inspection.status.get())
        a.camera.observation=(frame,{**data,'detection_frame_at':now},now)
        with patch.object(a.camera_inspection,'update_frame') as paint:
            a.draw_camera_frame(now,scheduled=True);a.draw_camera_frame(now+.21,scheduled=True)
            self.assertEqual(paint.call_count,2)
    def test_capture_can_refresh_detection_while_roi_tab_selected(self):
        frame,data=self.camera();a=self.app;a.camera_tabs.select(a.camera_inspection);self.root.update()
        a.camera.observation=(frame,data,time.monotonic());a.camera_display_snapshot=None
        path=a.save_camera()
        self.assertTrue(path.exists());self.assertIsNotNone(a.camera_display_snapshot['image'])
