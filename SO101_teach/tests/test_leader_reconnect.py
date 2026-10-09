import unittest,time,queue
from unittest.mock import patch,Mock
from types import SimpleNamespace
from dataclasses import replace
from so101_teach.domain import Snapshot
from so101_teach.motion import MotionSession,leader_target
from tests import test_ui as ui_tests

class LeaderReconnectTests(unittest.TestCase):
    setUp=ui_tests.UITests.setUp
    tearDown=ui_tests.UITests.tearDown
    def prepare(self):
        a=self.app
        a.profile['mode']='leader';a.profile['leader']={'port':'/dev/fake-leader','calibration_file':a.profile['calibration_file']}
        a.session=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);a.session.running=True;a.session.state='HOLD'
        self.addCleanup(lambda:setattr(a.session,'running',False))
        a.session.request=Mock();a.session.leader_calibration=a.calibration
        a.leader_session=SimpleNamespace(running=False,error='USB disconnected',latest=None,events=queue.Queue(),close=lambda:None)
        return a
    def snapshot(self,matched=True):
        a=self.app
        return Snapshot('leader',a.reference.middle.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,matched,'fake')
    def test_dead_leader_reconnects_read_only_then_requires_explicit_follow(self):
        a=self.prepare()
        with patch('so101_teach.ui.LeaderAssistSession') as factory:
            new=factory.return_value;new.running=True;new.latest=None;new.calibration=a.calibration
            a.prepare_leader_follow();factory.assert_called_once();new.start.assert_called_once();a.session.request.assert_not_called()
            self.assertEqual(factory.call_args.kwargs['role'],'leader');self.assertIn('leader-session-',str(factory.call_args.kwargs['audit_path']))
            new.latest=self.snapshot();a.prepare_leader_follow();a.session.request.assert_not_called()
            new.running=False
    def test_error_survives_dropped_event_and_is_reported_once(self):
        a=self.prepare();a.session.state='FOLLOW'
        with patch.object(a,'notice') as notice:
            a.poll_leader_status();a.poll_leader_status();notice.assert_called_once();self.assertIn('USB disconnected',notice.call_args.args[0])
    def test_missing_or_mismatched_leader_sample_never_requests_motion(self):
        a=self.prepare();a.leader_session.running=True
        with self.assertRaisesRegex(ValueError,'수신 대기'):a.prepare_leader_follow()
        a.leader_session.latest=self.snapshot(False)
        with self.assertRaisesRegex(ValueError,'보정 JSON'):a.prepare_leader_follow()
        a.session.request.assert_not_called();a.leader_session.running=False
    def test_outside_follower_limit_clamps_without_changing_saved_limits(self):
        a=self.prepare();a.leader_session.running=True;a.leader_session.latest=self.snapshot()
        cal=a.calibration;n='shoulder_lift';before=cal.motors[n];ticks={**a.reference.middle,n:before.low-11}
        a.leader_session.latest=replace(self.snapshot(),ticks=ticks)
        a.prepare_leader_follow();a.session.request.assert_not_called()
        mapped,limited=leader_target(ticks,cal,cal,with_limits=True)
        self.assertEqual(mapped[n],before.low+8);self.assertIn(n,limited);self.assertEqual(cal.motors[n],before)
        a.leader_session.running=False
    def test_reconnect_refuses_while_follower_is_moving(self):
        a=self.prepare();a.session.state='MOVING'
        with patch('so101_teach.ui.LeaderAssistSession') as factory:
            with self.assertRaisesRegex(ValueError,'현재 자세 유지'):a.motion_request('follow')
            factory.assert_not_called();a.session.request.assert_not_called()
