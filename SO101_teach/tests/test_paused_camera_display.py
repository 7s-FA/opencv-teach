"""Paused preview metadata must agree in the render and status update paths."""
import time
import unittest
from copy import deepcopy
from tests import test_camera_overview as fixture
from so101_teach.devices import CameraSession


class PausedCameraDisplayTests(unittest.TestCase):
    setUp=fixture.CameraOverviewTests.setUp
    tearDown=fixture.CameraOverviewTests.tearDown
    camera=fixture.CameraOverviewTests.camera

    def test_paused_video_keeps_status_and_adoption_until_resume(self):
        frame,data=self.camera();a=self.app
        camera=CameraSession(0,processor=a.detector.process)
        camera.running=True;a.camera=camera
        self.addCleanup(setattr,camera,'running',False)
        camera.observation=(frame,data,time.monotonic())
        a.toggle_jig_updates();self.assertTrue(a.jig_updates_paused)
        self.assertFalse(camera.processing_enabled)
        a.show_adoption.set(True);a.refresh_adoption_display()
        held=deepcopy(a.teaching_jig_results)
        for age in (0,2,30):
            now=time.monotonic();camera.observation=(frame,data,now-age)
            camera.preview_frame=(frame.copy(),now)
            preview=camera.preview_observation
            self.assertIsNone(preview[1]['detection_frame_at'])
            # poll() and draw_camera_frame() run at different frequencies.
            a.update_camera_overview(a.camera_view_results(preview[1]))
            before=[a.camera_jig_status.item(k,'values') for k in a.catalog.items]
            self.assertTrue(all(row[1:] == ('검출 정지','티칭 고정') for row in before))
            a.draw_camera_frame(time.monotonic())
            self.assertEqual([a.camera_jig_status.item(k,'values') for k in a.catalog.items],before)
            self.assertTrue(a.camera_canvas.find_withtag('image'))
            self.assertEqual(a.camera_display_snapshot['result']['by_jig'],{})
            self.assertEqual(a.teaching_jig_results,held)
        a.toggle_jig_updates();self.assertFalse(a.jig_updates_paused)
        self.assertTrue(camera.processing_enabled)
        data=deepcopy(data)
        data['live_by_jig']['pallet']={'selected':None,'candidates':[],'status':'not_found'}
        now=time.monotonic();camera.observation=(frame,data,now);camera.preview_frame=(frame,now)
        a.camera_overview_updated_at-=2;a.draw_camera_frame(time.monotonic())
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1],'미검출')
