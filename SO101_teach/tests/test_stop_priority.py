import unittest
from unittest.mock import Mock,patch
from tests import test_ui as fixtures
from so101_teach.motion import MotionSession
class StopPriorityTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):self.app.session=None;fixtures.UITests.tearDown(self)
    def session(self):
        a=self.app;s=MotionSession('unused',a.calibration);s.running=True;s.state='HOLD';s.request=Mock(return_value=17);a.session=s;return a,s
    def test_torque_release_precedes_failed_preview_cleanup(self):
        a,s=self.session()
        with patch.object(a,'stop_preview',side_effect=OSError('display offline')):
            a.stop_all()
        s.request.assert_called_once_with('release',None)
    def test_hold_precedes_failed_live_adjust_cleanup(self):
        a,s=self.session()
        with patch.object(a.live_adjust,'stop',side_effect=OSError('display offline')):
            self.assertEqual(a.motion_request('hold'),17)
        s.request.assert_called_once_with('hold',None)
    def test_failed_pre_motion_jig_freeze_sends_no_movement(self):
        a,s=self.session()
        with patch.object(a,'set_pose_frozen',side_effect=OSError('Pi freeze failed')):
            with self.assertRaises(OSError):a.motion_request('move',[a.target])
        s.request.assert_not_called()
    def test_post_dispatch_display_failure_does_not_lose_request_identity(self):
        a,s=self.session()
        with patch.object(a,'stop_preview',side_effect=OSError('display failed')):
            self.assertEqual(a.motion_request('move',[a.target]),17)
        s.request.assert_called_once_with('move',[a.target])

    def test_failed_stop_still_cancels_pending_execution(self):
        a,s=self.session();s.request.side_effect=OSError('ack missing');a.pending_execution={'page':'teach'}
        with self.assertRaises(OSError):a.motion_request('hold')
        self.assertIsNone(a.pending_execution);s.request.assert_called_once_with('hold',None)
    def test_live_toggle_off_sends_hold_before_preview_error(self):
        a,s=self.session();a.live_adjust.owner=a;a.live_adjust.request_id=12;s.completed_request_id=None
        with patch.object(a,'stop_preview',side_effect=OSError('camera offline')):
            with self.assertRaises(OSError):a.live_adjust.stop()
        s.request.assert_called_once_with('hold',None);self.assertIsNone(a.live_adjust.owner)
    def test_close_cancels_pending_space_release_callback(self):
        from types import SimpleNamespace
        a=self.app;a._space_down=True;a.space_key_release(SimpleNamespace());job=a._space_release_job
        self.assertIn(job,self.root.tk.call('after','info'));a.close()
        self.assertIsNone(a._space_release_job);self.assertNotIn(job,self.root.tk.call('after','info'))
    def test_close_continues_when_live_preview_cleanup_fails(self):
        a,s=self.session();s.close=Mock(side_effect=lambda:setattr(s,'running',False))
        with patch.object(a.live_adjust,'stop',side_effect=OSError('camera offline')):a.close()
        self.assertTrue(a.closed);s.close.assert_called_once()
