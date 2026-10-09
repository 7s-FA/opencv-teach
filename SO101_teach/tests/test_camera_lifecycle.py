import time,threading,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from copy import deepcopy
import numpy as np
from tests import test_ui as fixtures

class CameraLifecycleTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def camera(self):
        self.fake_jig_camera();a=self.app;c=a.camera
        c.close=Mock(side_effect=lambda:setattr(c,'running',False));c.start=Mock(side_effect=lambda:setattr(c,'running',True));c.stop=threading.Event()
        return c
    def test_startup_never_opens_camera_but_connects_requested_motors(self):
        a=self.app;a.camera_auto_view=True;a.startup_devices_pending=True;a.profile['mode']='follower'
        with patch.object(a,'start_camera') as start,patch.object(a,'connect_startup_devices') as connect:
            a.startup_connections()
        start.assert_not_called();connect.assert_called_once()
    def test_view_enter_and_leave_open_and_close_without_motor_commands(self):
        a=self.app;c=self.camera();c.running=False;a.camera_auto_view=True;a.profile['mode']='follower'
        with patch('so101_teach.ui.CameraSession',return_value=c):
            a.show_page('camera');a.show_page('camera');c.start.assert_called_once()
            a.show_page('teach');c.close.assert_called_once();self.assertTrue(a.camera_sleeping)
            a.show_page('camera');self.assertEqual(c.start.call_count,2);self.assertFalse(a.camera_sleeping)
    def test_manual_off_does_not_reopen_in_same_page(self):
        a=self.app;c=self.camera();a.camera_auto_view=True;a.profile['mode']='follower'
        a.show_page('camera');a.stop_camera()
        with patch.object(a,'start_camera') as start:
            a.show_page('camera');a.poll_camera_lifecycle();start.assert_not_called()
        self.assertFalse(c.running)
    def test_rapid_return_waits_for_previous_camera_owner_to_release(self):
        a=self.app;c=self.camera();a.camera_auto_view=True;a.profile['mode']='follower';c.close=Mock()
        a.show_page('camera');a.show_page('teach');a.show_page('camera')
        self.assertTrue(a.camera_restart_pending)
        with patch('so101_teach.ui.CameraSession',return_value=c) as factory:
            a.poll_camera_lifecycle();factory.assert_not_called()
            c.running=False;a.poll_camera_lifecycle();factory.assert_called_once();c.start.assert_called_once()
    def test_saved_pose_survives_idle_shutdown_only_until_hold_expiry(self):
        a=self.app;self.camera();at=a.camera.observation[2];a.suspend_camera()
        self.assertTrue(a.camera_results(at+5));self.assertFalse(a.camera_results(at+11))
        a.detector.freeze(True);self.assertTrue(a.camera_results(at+100))
        a.detector.freeze(False);self.assertFalse(a.camera_results(at+100))
    def test_reread_waits_for_new_measurement_then_returns_and_shuts_down(self):
        a=self.app;c=self.camera();a.request_jig_read();self.assertEqual(a.page,'camera')
        a.poll_camera_lifecycle();self.assertIsNotNone(a.camera_task)
        frame,result,_=c.observation;result['pose_measured_at']=time.monotonic();c.observation=(frame,result,time.monotonic())
        a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);self.assertEqual(a.page,'teach');self.assertFalse(c.running)
        self.assertIsNotNone(a.current_jig_reference())
    def test_timeout_and_cancel_release_capture(self):
        a=self.app;c=self.camera();a.request_jig_read();a.camera_task['started']-=10;a.camera_task['budget_started']-=10;c.observation=None
        a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);self.assertFalse(c.running);self.assertEqual(a.page,'teach')
        c=self.camera();a.camera_sleeping=False;a.camera_close_requested=False;a.request_jig_read();a.stop_preview()
        self.assertIsNone(a.camera_task);self.assertFalse(c.running);self.assertEqual(a.page,'teach')
    def test_execution_copies_fresh_pose_before_shutdown_and_never_uses_old_read(self):
        a=self.app;c=self.camera();a.follow_jig.set(True);a.commit_target();steps=deepcopy(a.episode['steps'])
        with patch.object(a,'begin_execution') as begin:
            a.prepare_execution('preview',steps);a.check_pending_execution();begin.assert_not_called()
            frame,result,_=c.observation;result['pose_measured_at']=time.monotonic();c.observation=(frame,result,time.monotonic())
            a.check_pending_execution();begin.assert_called_once();self.assertIsNotNone(begin.call_args.args[2]['pallet'])
            self.assertEqual(a.page,'teach');self.assertFalse(c.running);self.assertTrue(a.camera_sleeping)
        with patch.object(a,'start_camera'),patch.object(a,'begin_execution') as begin:
            a.prepare_execution('preview',steps);a.check_pending_execution();begin.assert_not_called()
    def test_photo_task_delivers_fresh_frame_and_turns_camera_off(self):
        a=self.app;c=self.camera();a.show_page('settings');a.camera_close_requested=False;c.running=True;a.camera_sleeping=False
        done=Mock();a.request_camera_photo(done);a.poll_camera_lifecycle();done.assert_not_called()
        c.observation=(np.zeros((20,20,3),np.uint8),None,time.monotonic());a.poll_camera_lifecycle()
        done.assert_called_once();self.assertFalse(c.running);self.assertEqual(a.page,'settings')
    def test_photo_failure_still_stops_camera(self):
        a=self.app;c=self.camera();done=Mock(side_effect=ValueError('board missing'));a.request_camera_photo(done)
        c.observation=(np.zeros((20,20,3),np.uint8),None,time.monotonic());a.poll_camera_lifecycle()
        self.assertFalse(c.running);self.assertIn('board missing',a.message.get())
    def test_leaving_measurement_view_early_still_closes_after_completion(self):
        a=self.app;c=self.camera();a.follow_jig.set(True);a.commit_target()
        with patch.object(a,'begin_execution'):
            a.prepare_execution('preview',deepcopy(a.episode['steps']));a.show_page('teach');self.assertTrue(c.running)
            frame,result,_=c.observation;result['pose_measured_at']=time.monotonic();c.observation=(frame,result,time.monotonic())
            a.check_pending_execution();self.assertFalse(c.running)
    def test_leaving_measurement_view_early_still_closes_on_timeout(self):
        a=self.app;c=self.camera();a.follow_jig.set(True);a.commit_target()
        a.prepare_execution('preview',deepcopy(a.episode['steps']));a.show_page('teach');c.observation=None;a.pending_execution['started']-=10;a.pending_execution['budget_started']-=10
        a.check_pending_execution();self.assertFalse(c.running);self.assertIsNone(a.pending_execution)
    def test_manual_connect_can_queue_reopen_even_with_auto_view_disabled(self):
        a=self.app;c=self.camera();a.camera_auto_view=False;a.show_page('camera');c.close=Mock();a.stop_camera()
        a.start_camera();self.assertTrue(a.camera_restart_pending);self.assertTrue(a.camera_needed())
        with patch('so101_teach.ui.CameraSession',return_value=c):
            c.running=False;a.poll_camera_lifecycle();c.start.assert_called_once()
