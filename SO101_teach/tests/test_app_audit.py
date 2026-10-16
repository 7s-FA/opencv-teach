"""Regression checks for camera races and stable desktop repainting."""
import time,unittest
from unittest.mock import patch
from tests import test_capture_gallery as fixtures

class CameraAuditTests(unittest.TestCase):
    setUp=fixtures.CaptureGalleryTests.setUp
    tearDown=fixtures.CaptureGalleryTests.tearDown
    prepare=fixtures.CaptureGalleryTests.prepare

    def test_frame_arriving_after_tick_start_is_displayed_not_cleared(self):
        p=self.prepare();a=self.app;frame,data,_=a.camera.observation
        started=time.monotonic()-.01;a.camera.observation=(frame,data,time.monotonic())
        a.camera_tabs.select(0);a.draw_camera_frame(started)
        self.assertIsNotNone(a.camera_display_snapshot)
        a.camera_tabs.select(p);p.update_frame(started)
        self.assertIsNotNone(p.live_image)

    def test_disconnect_during_capture_reports_retry_and_writes_no_files(self):
        p=self.prepare();a=self.app;obs=a.camera.observation
        a.camera_display_snapshot=None
        with patch.object(a,'camera_view_observation',side_effect=[obs,None]):
            with self.assertRaisesRegex(ValueError,'새 영상'):a.save_camera()
        self.assertFalse((self.data/'captures').exists())
        self.assertIsNone(p.last_capture_at)

    def test_unchanged_gallery_frame_keeps_image_and_control_state(self):
        p=self.prepare();a=self.app
        a.camera.observation=(*a.camera.observation[:2],time.monotonic())
        now=time.monotonic();p.update_frame(now)
        image=p.live_canvas.photo;items=p.live_canvas.find_withtag('image')
        with patch.object(p.save_button,'configure',wraps=p.save_button.configure) as button,patch.object(p.live_status,'set',wraps=p.live_status.set) as status:
            for _ in range(30):p.update_frame(now)
        self.assertEqual(button.call_count,0);self.assertEqual(status.call_count,0)
        self.assertIs(p.live_canvas.photo,image);self.assertEqual(p.live_canvas.find_withtag('image'),items)

    def test_refresh_does_not_reselect_existing_photo(self):
        p=self.prepare();a=self.app;a.save_camera()
        with patch.object(p.table,'selection_set',wraps=p.table.selection_set) as select:
            p.refresh();p.refresh()
        self.assertEqual(select.call_count,0)

    def test_long_messages_remain_readable_without_moving_controls(self):
        p=self.prepare();a=self.app;self.root.geometry('1920x1043');self.root.update()
        bottom=a.arm_btn.winfo_rooty();long='카메라 연결 오류 · 새 영상을 기다리세요.\n'*20
        a.notice(long,True);self.root.update()
        self.assertEqual(a.message_label.get('1.0','end-1c'),long)
        self.assertEqual(a.arm_btn.winfo_rooty(),bottom)
        a.message_label.yview_moveto(1);self.assertGreater(a.message_label.yview()[0],0)
        a.camera=None;a.camera_status.set(long);a.show_page('camera');a.camera_tabs.select(0);self.root.update()
        self.assertEqual(a.camera_status_label.text.get('1.0','end-1c'),long)
        a.camera_status_label.text.yview_moveto(1);self.assertGreater(a.camera_status_label.text.yview()[0],0)
