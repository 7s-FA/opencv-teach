"""Connection loss and rejection must require a fresh explicit user request."""
import unittest,time
from types import SimpleNamespace
from unittest.mock import Mock,patch
from concurrent.futures import Future
from tests import test_ui as fixtures

class ManualReconnectTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def broken(self):
        a=self.app;a.remote_mode=True
        a.remote=SimpleNamespace(error='timed out',close=Mock())
        a.session=SimpleNamespace(running=False,close=Mock());a.leader_session=None
        return a,a.settings.pi_panel

    def assert_no_later_retry(self,p):
        with patch.object(p,'connect') as connect,patch('so101_teach.pi_settings_ui.time.monotonic',return_value=10**12):
            # Include obsolete retry flags: neither time nor an external job
            # finishing can turn a failed request into new control ownership.
            p.retry_at=0
            for _ in range(20):p.poll()
            connect.assert_not_called()
        self.assertFalse(p.auto_enabled)

    def test_loss_cancels_pending_motion_without_reacquiring(self):
        a,p=self.broken();future=Future();a.remote_plan_job=(future,('play',[],None),a.episode['id']);a.pending_execution={'page':'teach'};a.safe_entry={'request_id':1}
        with patch.object(a,'motion_request',side_effect=AssertionError('no motor replay')):
            p.poll()
            self.assertIsNone(a.remote_plan_job);self.assertIsNone(a.pending_execution);self.assertIsNone(a.safe_entry)
            self.assertFalse(p.restore_devices);self.assert_no_later_retry(p)

    def test_rejected_initial_request_never_retries(self):
        a=self.app;a.remote_mode=True;p=a.settings.pi_panel
        f=Future();f.set_exception(ValueError('다른 PC 창이 Pi를 사용 중입니다.'));p.job=f;p.job_kind='connect'
        p.poll();self.assertIn('자동 재시도 없음',p.status.get());self.assert_no_later_retry(p)
        self.assertFalse(a.startup_devices_pending)

    def test_stale_automatic_callback_cannot_open_a_link(self):
        a,p=self.broken();p.schedule_recovery()
        with patch('so101_teach.remote_client.RemoteLink') as link:
            p.connect(automatic=True);link.assert_not_called()
        self.assertIsNone(p.job)

    def test_successful_manual_install_adopts_hold_without_motion(self):
        a,p=self.broken();p.schedule_recovery()
        follower={'running':True,'state':'HOLD','error':None,'index':0,'request_id':3,'completed_request_id':3,'gripper_hold_tick':None,'program_active':False,'command_pending':False,'latest':None}
        link=SimpleNamespace(error=None,latest_state={'follower':follower,'server_now':time.monotonic()},listeners={},config={'host':'pi'},health={'devices':{'serial':[],'cameras':[]}},rpc=Mock(),close=Mock(),devices_prepared=True,restored_leader=None,restored_camera=None)
        p.install(link)
        self.assertFalse(p.recovery_pending);self.assertFalse(p.auto_enabled)
        self.assertEqual(a.session.state,'HOLD');link.rpc.assert_not_called();a.session.running=False

    def test_explicit_request_connects_without_motion_or_follow_commands(self):
        a,p=self.broken();p.schedule_recovery();a.profile['mode']='follower'
        state={'follower':None,'server_now':time.monotonic()}
        link=SimpleNamespace(error=None,listeners={},open=Mock(),sync_bundle=Mock(),rpc=Mock(),http=Mock(return_value=state),close=Mock(),leader_client=None,lease='test',event_id=0)
        with patch('so101_teach.remote_client.RemoteLink',return_value=link):
            p.connect(control_requested=True);result=p.job.result(timeout=3)
        self.assertIs(result,link);methods=[c.args[0] for c in link.rpc.call_args_list]
        self.assertIn('connect',methods);self.assertNotIn('command',methods);self.assertNotIn('cal_start',methods)
        p.job=None
