import time,unittest
from copy import deepcopy
from so101_teach.camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
from unittest.mock import patch
from tests import test_ui as fixtures

class PauseAdoptionLatencyTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def delayed(self):
        now=time.monotonic();self.fake_jig_camera(measured=now-.2);a=self.app;f,d,_=a.camera.observation;a.camera.observation=(f,d,now-(MEASUREMENT_MAX_AGE_SECONDS+.2));a.camera_sleeping=False
        return a,now
    def test_visible_accepted_pallet_is_fixed_without_unnecessary_reread(self):
        a,now=self.delayed();self.assertEqual(a.camera_results(now),{});self.assertTrue(a.camera_adoption_results(now)['pallet']['selected'])
        before=deepcopy(a.episode)
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_not_called();self.assertEqual(a.current_jig_reference()['pose'],[228,-138,67]);self.assertEqual(a.episode,before)
        with patch('so101_teach.camera_lifecycle.time.monotonic',return_value=now+90):self.assertEqual(a.current_jig_reference()['pose'],[228,-138,67])
    def test_expired_invalidated_and_failed_camera_still_request_new_read(self):
        for kind in ['expired','invalidated','failure','recovering','future']:
            a,now=self.delayed();a.jig_updates_paused=False;a.teaching_jig_results.clear();a.measurement_valid_after=0
            if kind=='expired':a.camera.observation[1]['pose_measured_at']=now-11
            elif kind=='invalidated':a.measurement_valid_after=now-.1
            elif kind=='failure':a.camera.error='disconnected'
            elif kind=='recovering':a.camera.recovering=True
            else:a.camera.observation[1]['pose_measured_at']=now+1
            with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
            read.assert_called_once();self.assertFalse(a.teaching_jig_results,kind)
    def test_resume_never_reuses_previous_accepted_basis(self):
        a,now=self.delayed();a.teaching_resume_at=now-.1
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_called_once();self.assertFalse(a.teaching_jig_results)
    def test_pause_reread_accepts_new_consensus_despite_processing_delay(self):
        a,now=self.delayed();a.jig_updates_paused=True;a.measurement_valid_after=now-4
        a.camera_task={'kind':'jig','jig':'pallet','started':now-4,'page':'camera'}
        with patch.object(a,'motion_request') as move:a.poll_camera_lifecycle()
        move.assert_not_called();self.assertIsNone(a.camera_task);self.assertEqual(a.current_jig_reference()['pose'],[228,-138,67])
        self.assertEqual(a.camera.observation[2],now-(MEASUREMENT_MAX_AGE_SECONDS+.2))
    def test_pause_reread_never_uses_pre_request_adoption(self):
        a,now=self.delayed();a.jig_updates_paused=True;a.camera_task={'kind':'jig','jig':'pallet','started':now-.1,'page':'camera'}
        a.poll_camera_lifecycle();self.assertIsNotNone(a.camera_task);self.assertFalse(a.teaching_jig_results)
    def test_regular_measurement_and_follow_preparation_keep_fresh_frame_gate(self):
        a,now=self.delayed();a.camera_task={'kind':'jig','jig':'pallet','started':now-4,'page':'camera'}
        a.poll_camera_lifecycle();self.assertIsNotNone(a.camera_task);self.assertEqual(a.camera_results(now),{})
