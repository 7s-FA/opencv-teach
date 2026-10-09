import time,unittest,queue
from dataclasses import replace
from unittest.mock import Mock,patch
from types import SimpleNamespace
from tests.test_camera_lifecycle import CameraLifecycleTests
from so101_teach.motion import MotionSession
from so101_teach.domain import Snapshot
class FollowMeasureTests(unittest.TestCase):
    setUp=CameraLifecycleTests.setUp
    fake_jig_camera=CameraLifecycleTests.fake_jig_camera
    camera=CameraLifecycleTests.camera
    def tearDown(self):
        self.app.session=None;self.app.leader_session=None
        CameraLifecycleTests.tearDown(self)
    def ready(self):
        a=self.app;a.profile['mode']='leader';c=self.camera()
        s=MotionSession('unused',a.calibration);s.running=True;s.state='HOLD';s.request=Mock();a.session=s
        a.leader_session=SimpleNamespace(running=True,calibration=a.calibration,latest=Snapshot('leader',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake'),error=None,events=queue.Queue(),begin_assist=Mock(),stop_assist=Mock())
        return a,s,c
    def fresh(self,c):
        frame,result,_=c.observation;result['pose_measured_at']=time.monotonic();c.observation=(frame,result,time.monotonic())
    def start(self,a):
        with patch.object(a,'start_camera'):
            a.motion_request('follow')
    def test_detect_freeze_then_follow_once(self):
        a,s,c=self.ready();self.start(a);s.request.assert_not_called();a.poll_camera_lifecycle();s.request.assert_not_called()
        self.fresh(c);s.request.side_effect=lambda *args:self.assertTrue(a.jig_updates_paused)
        a.poll_camera_lifecycle();a.poll_camera_lifecycle();s.request.assert_called_once_with('follow')
        self.assertEqual(a.page,'teach');self.assertTrue(a.teaching_jig_results);self.assertFalse(c.running)
    def test_paused_follow_reuses_held_position_without_reopening_camera(self):
        a,s,c=self.ready();a.pause_jig_updates_for_teaching();held=a.held_jig_reference('pallet');c.observation=None
        with patch.object(a,'start_camera') as start:self.start(a);a.poll_camera_lifecycle();start.assert_not_called()
        s.request.assert_called_once_with('follow');self.assertEqual(a.held_jig_reference('pallet'),held)
    def test_timeout_never_falls_back_to_old_pose(self):
        a,s,c=self.ready();self.start(a);c.observation=None;a.camera_task['started']-=100;a.camera_task['budget_started']-=100
        a.poll_camera_lifecycle();s.request.assert_not_called();self.assertIsNone(a.camera_task)
    def test_camera_error_cancels(self):
        a,s,c=self.ready();self.start(a);c.error='camera failed';a.poll_camera_lifecycle();s.request.assert_not_called();self.assertIsNone(a.camera_task)
    def test_navigation_cancels(self):
        a,s,c=self.ready();self.start(a);a.show_page('teach');self.fresh(c);a.poll_camera_lifecycle();s.request.assert_not_called();self.assertIsNone(a.camera_task)
    def test_stop_cancels(self):
        a,s,c=self.ready();self.start(a);a.motion_request('hold');self.fresh(c);a.poll_camera_lifecycle();self.assertEqual(s.request.call_args.args[0],'hold');self.assertEqual(s.request.call_count,1)
    def test_replaced_connection_cancels(self):
        a,s,c=self.ready();self.start(a);a.session=SimpleNamespace(running=True,state='HOLD');self.fresh(c);a.poll_camera_lifecycle();s.request.assert_not_called()
    def test_leader_waits_until_after_detection(self):
        a,s,c=self.ready();a.leader_session=None
        with patch.object(a,'prepare_leader_follow',return_value=False) as prepare:
            self.start(a);a.poll_camera_lifecycle();prepare.assert_not_called()
            self.fresh(c);a.poll_camera_lifecycle();prepare.assert_called_once();s.request.assert_not_called()
            a.camera_task['captured']-=11;a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);s.request.assert_not_called()
    def test_leader_sample_arrives_after_detection(self):
        a,s,c=self.ready();sample=a.leader_session.latest;a.leader_session.latest=None;self.start(a);self.fresh(c);a.poll_camera_lifecycle();s.request.assert_not_called()
        a.leader_session.latest=sample;a.poll_camera_lifecycle();s.request.assert_called_once()
    def test_invalid_calibration_cannot_start(self):
        a,s,c=self.ready();a.leader_session.latest=replace(a.leader_session.latest,calibration_matches=False);self.start(a);self.fresh(c);a.poll_camera_lifecycle();s.request.assert_not_called();self.assertIsNone(a.camera_task)
    def test_duplicate_click_cannot_create_second_intent(self):
        a,s,c=self.ready();self.start(a)
        with self.assertRaises(ValueError):self.start(a)
        self.fresh(c);a.poll_camera_lifecycle();s.request.assert_called_once()

    def test_unaccepted_detection_waits_without_cancelling_follow_intent(self):
        a,s,c=self.ready();self.start(a);frame,_,_=c.observation
        for status in ('confirming','not_found','ambiguous'):
            c.observation=(frame,{'selected':None,'candidates':[],'status':status,'pose_measured_at':None},time.monotonic())
            a.poll_camera_lifecycle()
            self.assertIsNotNone(a.camera_task,status);s.request.assert_not_called();a.leader_session.begin_assist.assert_not_called()
        self.fake_jig_camera();fresh=a.camera.observation;frame,result,_=fresh
        result['pose_measured_at']=time.monotonic();c.observation=(frame,result,time.monotonic());a.camera=c
        a.poll_camera_lifecycle();s.request.assert_called_once_with('follow')

    def test_unaccepted_detection_times_out_with_measurement_message(self):
        a,s,c=self.ready();self.start(a);frame,_,_=c.observation
        c.observation=(frame,{'selected':None,'candidates':[],'status':'confirming','pose_measured_at':None},time.monotonic())
        a.camera_task['started']-=a.measurement_timeout_seconds+1;a.camera_task['budget_started']-=a.measurement_timeout_seconds+1
        a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);s.request.assert_not_called()
        self.assertIn('지그 측정 실패',a.message.get());self.assertNotIn('NoneType',a.message.get())
