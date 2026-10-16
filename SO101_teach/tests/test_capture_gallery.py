import time,unittest
from unittest.mock import patch
import cv2
import numpy as np
from . import test_ui as fixtures


class CaptureGalleryTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def prepare(self):
        fixtures.UITests.fake_jig_camera(self)
        a=self.app
        frame,_,at=a.camera.observation;a.camera.observation=(frame,{'selected':None,'candidates':[]},at)
        a.show_page('camera');a.camera_tabs.select(a.camera_gallery);self.root.update()
        return a.camera_gallery

    def test_live_and_saved_photo_are_independent_and_new_capture_wins(self):
        p=self.prepare();a=self.app
        frame,results,_=a.camera.observation
        frame[:]=(0,0,220);a.camera.observation=(frame,results,time.monotonic())
        a.draw_camera_frame(time.monotonic());first=a.save_camera()
        self.assertEqual(p.selected,first.name)
        np.testing.assert_array_equal(np.asarray(p.saved_image)[0,0],[220,0,0])
        frame=np.full_like(frame,(0,200,0));a.camera.observation=(frame,results,time.monotonic())
        a.draw_camera_frame(time.monotonic())
        np.testing.assert_array_equal(np.asarray(p.saved_image)[0,0],[220,0,0])
        p.capture_ready_at=0
        second=a.save_camera();self.assertEqual(p.selected,second.name)
        p.table.selection_set(first.name);p.select_photo();p.refresh()
        self.assertEqual(p.selected,first.name)
        p.capture_ready_at=0;a.camera.observation=(frame,results,time.monotonic())
        third=a.save_camera();self.assertEqual(p.selected,third.name)
        self.assertEqual(len(p.entries),3)
        np.testing.assert_array_equal(cv2.imread(str(third.parent.parent/'원본'/third.name)),frame)

    def test_offline_history_persists_and_missing_variant_is_reported(self):
        p=self.prepare();a=self.app;path=a.save_camera();a.camera=None
        p.update_frame(time.monotonic())
        self.assertFalse(p.save_button.instate(['disabled']));self.assertIsNotNone(p.saved_image)
        self.assertIsNone(p.live_image)
        path.unlink();p.variant.set('화면 표시본');p.load_selected()
        self.assertIsNone(p.saved_image);self.assertIn('읽을 수 없습니다',p.saved_status.get())

    def test_external_new_photo_selected_without_refresh_stealing_old_selection(self):
        p=self.prepare();a=self.app;first=a.save_camera()
        raw=p.folder/'원본'/'new.png';cv2.imwrite(str(raw),np.full((10,20,3),100,np.uint8));p.refresh()
        self.assertEqual(p.selected,'new.png')
        p.table.selection_set(first.name);p.select_photo();p.refresh()
        self.assertEqual(p.selected,first.name)

    def test_maximized_layout_shows_both_views_and_list(self):
        p=self.prepare();self.root.geometry('1920x1043');self.root.update()
        for canvas in p.canvases:
            self.assertTrue(canvas.winfo_ismapped());self.assertGreater(canvas.winfo_width(),600)
        self.assertGreater(p.saved_canvas.winfo_rootx(),p.live_canvas.winfo_rootx())
        self.assertLess(p.table.winfo_rooty()+p.table.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())

    def test_repeated_clicks_and_reused_frame_do_not_create_duplicates(self):
        p=self.prepare();a=self.app;frame,result,_=a.camera.observation
        a.camera.observation=(frame,result,time.monotonic());a.draw_camera_frame(time.monotonic())
        p.save_button.invoke()
        self.assertEqual(len(p.entries),1)
        self.assertFalse(p.save_button.instate(['disabled']))
        for _ in range(5):p.save_button.invoke()
        self.assertEqual(len(p.entries),1)
        with self.assertRaisesRegex(ValueError,'중복'):a.save_camera()
        # Even a new frame must respect the 0.2-second click debounce.
        a.camera.observation=(frame.copy(),result,time.monotonic())
        with self.assertRaisesRegex(ValueError,'중복'):a.save_camera()
        # After the debounce, the original saved frame still cannot be reused.
        at=p.last_capture_at;p.capture_ready_at=0
        a.camera.observation=(frame,result,at)
        p.update_frame(time.monotonic())
        self.assertFalse(p.save_button.instate(['disabled']))
        with self.assertRaisesRegex(ValueError,'새 영상'):a.save_camera()
        a.camera.observation=(frame.copy(),result,time.monotonic())
        p.update_frame(time.monotonic());self.assertFalse(p.save_button.instate(['disabled']))
        p.save_button.invoke();self.assertEqual(len(p.entries),2)
        self.assertEqual(len(list((p.folder/'원본').glob('*.png'))),2)

    def test_failed_write_does_not_consume_capture_or_leave_files(self):
        p=self.prepare();a=self.app
        with patch('cv2.imwrite',return_value=False):
            with self.assertRaises(OSError):a.save_camera()
        self.assertIsNone(p.last_capture_at)
        self.assertEqual(list(p.folder.rglob('*.png')),[])
        a.save_camera();self.assertEqual(len(p.entries),1)
