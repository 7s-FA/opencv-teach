import unittest,time,queue
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests import test_taught_scene as fixtures
from so101_teach.domain import Snapshot
from so101_teach.motion import MotionSession
class EpisodeJigVisibilityTests(unittest.TestCase):
    setUp=fixtures.TaughtSceneTests.setUp
    fake_jig_camera=fixtures.TaughtSceneTests.fake_jig_camera
    teach=fixtures.TaughtSceneTests.teach
    def tearDown(self):
        self.app.session=None;self.app.leader_session=None;fixtures.TaughtSceneTests.tearDown(self)
    def follower(self):
        a=self.app;a.profile['mode']='leader';a.preferences['leader_gravity_assist']='사용 안 함';s=MotionSession('unused',a.calibration);s.running=True;s.state='HOLD';a.session=s
        a.leader_session=SimpleNamespace(running=True,calibration=a.calibration,latest=Snapshot('leader',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake'),error=None,events=queue.Queue())
        s.request=Mock(return_value=1);return a,s
    def follow(self,a):
        with patch.object(a,'start_camera'),patch.object(a,'suspend_camera'):
            a.motion_request('follow')
            if a.camera and a.camera.observation:
                frame,result,_=a.camera.observation;result['pose_measured_at']=time.monotonic();a.camera.observation=(frame,result,time.monotonic());a.camera_sleeping=False;a.poll_camera_lifecycle()
    def test_fixed_step_keeps_all_episode_jigs_without_camera(self):
        a=self.teach();other=deepcopy(a.episode['steps'][0]);other['id']='other';other['jig_id']='other-jig';other['jig_reference']['pose']=[10,20,30];a.episode['steps'].append(other)
        a.follow_jig.set(False);a.commit_target();a.camera=None;before=deepcopy(a.episode)
        refs=a.saved_scene_references();self.assertEqual(set(refs),{'pallet','other-jig'});self.assertEqual(refs['pallet']['pose'],[228,-138,67]);self.assertEqual(a.episode,before)
    def test_following_step_overrides_its_jig_but_keeps_the_other_jig(self):
        a=self.teach();other=deepcopy(a.episode['steps'][0]);other['id']='other';other['jig_id']='other-jig';other['jig_reference']['pose']=[10,20,30];a.episode['steps'].append(other)
        self.assertEqual(set(a.saved_scene_references()),{'pallet','other-jig'})
    def test_follow_command_observes_pause_already_set_and_pose_does_not_change(self):
        a,s=self.follower();self.fake_jig_camera((228,-138,67));old=a.current_jig_reference()
        s.request.side_effect=lambda *args: self.assertTrue(a.jig_updates_paused) or 1
        self.follow(a);self.fake_jig_camera((300,-50,20));self.assertEqual(a.current_jig_reference(),old)
        s.request.assert_called_once_with('follow');self.assertIn('갱신 정지 중',a.jig_pause_btn['text'])
    def test_already_paused_is_not_resumed_or_replaced(self):
        a,s=self.follower();self.fake_jig_camera();a.toggle_jig_updates();old=deepcopy(a.teaching_jig_results)
        self.fake_jig_camera((300,-50,20));self.follow(a);self.assertTrue(a.jig_updates_paused);self.assertEqual(a.teaching_jig_results,old)
    def test_follow_without_detection_pauses_without_inventing_pose(self):
        a,s=self.follower();self.follow(a);self.assertFalse(a.jig_updates_paused);self.assertIsNone(a.current_jig_reference());s.request.assert_not_called()
    def test_invalid_leader_does_not_change_pause_or_move(self):
        a,s=self.follower();a.leader_session.latest=None
        self.follow(a)
        self.assertFalse(a.jig_updates_paused);s.request.assert_not_called()
    def test_pending_measurement_is_not_overlapped_by_follow(self):
        a,s=self.follower();a.camera_task={'started':time.monotonic(),'page':'teach'}
        with self.assertRaises(ValueError):self.follow(a)
        s.request.assert_not_called();self.assertFalse(a.jig_updates_paused);a.camera_task=None

    def test_follow_displays_detected_pose_in_step_view_even_after_selection(self):
        a=self.teach();a,s=self.follower();self.follow(a);s.state='FOLLOW'
        a.apply_target(a.target);a.mode='target'
        self.assertIsNone(a.saved_scene_references());self.assertEqual(a.measured_scene_references()['pallet']['pose'],[250,-90,12])
        self.fake_jig_camera((300,-50,20));self.assertEqual(a.measured_scene_references()['pallet']['pose'],[250,-90,12])
    def test_follow_without_detection_never_uses_saved_pose_as_detected(self):
        a=self.teach();a,s=self.follower();a.camera=None;self.follow(a);s.state='FOLLOW';a.apply_target(a.target)
        self.assertIsNone(a.saved_scene_references());self.assertIsNone(a.measured_scene_references());s.request.assert_not_called()
    def test_follow_stop_and_new_edit_restore_episode_saved_scene(self):
        a=self.teach();a,s=self.follower();self.follow(a);s.state='HOLD';a.apply_target(a.target)
        self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])

    def test_explicit_calculated_preview_keeps_its_own_reference_during_follow(self):
        a=self.teach();a,s=self.follower();self.follow(a);s.state='FOLLOW';a.mode='target'
        ref=deepcopy(a.current_jig_reference());ref['pose']=[300,-100,40];a.preview_jig_references={'pallet':ref}
        self.assertEqual(a.measured_scene_references()['pallet']['pose'],[300,-100,40])
