import unittest,time,queue
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_ui as fixtures
from so101_teach.domain import Snapshot
from so101_teach.motion import MotionSession

class LeaderBindingTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        self.app.session=None;self.app.leader_session=None;fixtures.UITests.tearDown(self)
    def sample(self):
        a=self.app;return Snapshot('leader',a.reference.middle.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
    def reader(self):return SimpleNamespace(calibration=self.app.calibration,running=True,latest=self.sample(),close=Mock(),events=queue.Queue(),error=None)
    def test_restore_binds_calibration_and_provider_without_starting_motion(self):
        a=self.app;a.remote_mode=True;a.profile['mode']='leader';reader=self.reader()
        follower={'running':True,'state':'HOLD','error':None,'index':0,'request_id':None,'completed_request_id':None,'gripper_hold_tick':None,'program_active':False,'command_pending':False,'latest':None}
        link=SimpleNamespace(error=None,latest_state={'follower':follower,'server_now':time.monotonic()},listeners={},config={'host':'pi'},health={'devices':{'serial':[],'cameras':[]}},rpc=Mock(return_value={}),close=Mock(),devices_prepared=True,automatic_recovery=True,restored_leader=reader,restored_camera=None)
        a.settings.pi_panel.install(link)
        self.assertIs(a.session.leader_calibration,a.calibration);self.assertIs(a.session.leader_provider(),reader.latest);link.rpc.assert_not_called()
        a.prepare_leader_follow();link.rpc.assert_not_called()
    def test_follow_preflight_repairs_missing_binding_on_existing_session(self):
        a=self.app;a.profile['mode']='leader';a.session=MotionSession('unused',a.calibration);a.session.running=True;a.session.state='HOLD';a.session.request=Mock();a.leader_session=self.reader()
        self.assertIsNone(a.session.leader_calibration);a.prepare_leader_follow()
        self.assertIs(a.session.leader_calibration,a.leader_session.calibration);a.session.request.assert_not_called()
    def test_missing_calibration_reports_actionable_error_without_command(self):
        a=self.app;a.profile['mode']='leader';a.session=MotionSession('unused',a.calibration);a.session.running=True;a.session.state='HOLD';a.session.request=Mock();a.leader_session=self.reader();a.leader_session.calibration=None
        with self.assertRaisesRegex(ValueError,'리더 보정 정보'):a.prepare_leader_follow()
        a.session.request.assert_not_called()
    def test_snapshot_is_read_once_during_preflight(self):
        a=self.app;a.profile['mode']='leader';a.session=MotionSession('unused',a.calibration);a.session.running=True;a.session.state='HOLD';a.session.request=Mock();sample=self.sample()
        class Reader:
            running=True;calibration=a.calibration;reads=0
            @property
            def latest(self):
                self.reads+=1
                return sample if self.reads==1 else None
        a.leader_session=Reader();a.prepare_leader_follow();self.assertEqual(a.leader_session.reads,1);a.session.request.assert_not_called()
    def test_rebinding_uses_new_reader_and_stops_using_old_source(self):
        a=self.app;a.session=MotionSession('unused',a.calibration);old=self.reader();a.leader_session=old;a.bind_leader_connection();new=self.reader();a.leader_session=new;a.bind_leader_connection()
        self.assertIs(a.session.leader_provider(),new.latest);new.running=False;self.assertIsNone(a.session.leader_provider());self.assertTrue(old.running)
