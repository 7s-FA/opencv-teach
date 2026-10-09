import unittest,time,threading
from types import SimpleNamespace
from unittest.mock import Mock,patch
from concurrent.futures import Future
from tests import test_ui as fixtures
from so101_teach.domain import atomic_json

class AutoReconnectTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def broken(self,intent=True):
        a=self.app;a.remote_mode=True
        a.remote=SimpleNamespace(error='timed out',follower_requested=intent,latest_state={'follower':{'running':True}},close=Mock())
        a.session=SimpleNamespace(running=False,close=Mock());a.leader_session=None
        return a,a.settings.pi_panel
    def test_loss_cancels_pending_motion_then_schedules_one_retry(self):
        a,p=self.broken();future=Future();a.remote_plan_job=(future,('play',[],None),a.episode['id']);a.pending_execution={'page':'teach'};a.safe_entry={'request_id':1}
        with patch.object(a,'motion_request',side_effect=AssertionError('no motor replay')):
            p.poll();self.assertTrue(p.recovery_pending);self.assertTrue(p.restore_devices)
            self.assertIsNone(a.remote_plan_job);self.assertIsNone(a.pending_execution);self.assertIsNone(a.safe_entry)
            with patch.object(p,'connect') as connect:
                p.poll();connect.assert_not_called();p.retry_at=0;p.poll();connect.assert_called_once_with(automatic=True)
    def test_manual_follower_disconnect_is_not_turned_into_connect_intent(self):
        a,p=self.broken(False);p.schedule_recovery();self.assertFalse(p.restore_devices)
    def test_failed_retries_back_off_and_continue_without_limit(self):
        a,p=self.broken();p.schedule_recovery()
        for attempt,delay in enumerate((1,2,5,10,10,10),1):
            f=Future();f.set_exception(OSError('offline'));p.job=f;p.job_kind='reconnect'
            with patch('so101_teach.pi_settings_ui.time.monotonic',return_value=100.):p.poll()
            self.assertEqual(p.retry_count,attempt);self.assertEqual(p.retry_at,100.+delay);self.assertTrue(p.auto_enabled)
    def test_closed_or_local_app_does_not_retry(self):
        a,p=self.broken();p.schedule_recovery();p.retry_at=0
        with patch.object(p,'connect') as connect:
            a.closed=True;p.poll();a.closed=False;a.remote_mode=False;p.poll();connect.assert_not_called()
    def test_inflight_job_prevents_duplicate_connect(self):
        a,p=self.broken();p.schedule_recovery();p.retry_at=0;p.job=Future()
        with patch.object(p,'connect') as connect:p.poll();connect.assert_not_called()
        p.job.cancel();p.job=None
    def test_success_clears_retry_state_and_adopts_hold_without_commands(self):
        a,p=self.broken();p.schedule_recovery();p.retry_count=4
        follower={'running':True,'state':'HOLD','error':None,'index':0,'request_id':3,'completed_request_id':3,'gripper_hold_tick':None,'program_active':False,'command_pending':False,'latest':None}
        link=SimpleNamespace(error=None,latest_state={'follower':follower,'server_now':time.monotonic()},listeners={},config={'host':'pi'},health={'devices':{'serial':[],'cameras':[]}},rpc=Mock(),close=Mock(),devices_prepared=True,automatic_recovery=True,restored_leader=None,restored_camera=None)
        p.install(link)
        self.assertFalse(p.recovery_pending);self.assertEqual(p.retry_count,0);self.assertEqual(a.session.state,'HOLD');self.assertTrue(a.session.running)
        link.rpc.assert_not_called();a.session.running=False
    def test_failed_connection_work_never_calls_move_arm_or_follow(self):
        a,p=self.broken();p.schedule_recovery();p.restore_devices=True;a.profile['mode']='follower'
        config={'host':'pi','user':'robot','port':22,'identity_file':'','app_dir':'~/OpenCV_teach','python':'python3'};atomic_json(self.data/'pi-connection.json',config)
        state={'follower':None,'server_now':time.monotonic()}
        link=SimpleNamespace(error=None,config=config,listeners={},open=Mock(),sync_bundle=Mock(),rpc=Mock(),http=Mock(return_value=state),close=Mock(),leader_client=None,lease='test',event_id=0)
        with patch('so101_teach.remote_client.RemoteLink',return_value=link):
            p.connect(automatic=True);result=p.job.result(timeout=2)
        self.assertIs(result,link);methods=[c.args[0] for c in link.rpc.call_args_list]
        self.assertIn('connect',methods);self.assertNotIn('command',methods);self.assertNotIn('cal_start',methods);self.assertNotIn('disconnect',methods)
        p.job=None
    def test_manual_disconnect_clears_restore_intent(self):
        from so101_teach.remote_client import RemoteMotionSession
        link=SimpleNamespace(rpc=Mock(),listeners={});session=RemoteMotionSession(link,'Pi',self.app.calibration);session.start();self.assertTrue(link.follower_requested)
        session.close();self.assertFalse(link.follower_requested);self.assertEqual(link.rpc.call_args.args[0],'disconnect')
    def test_failed_initial_connect_is_also_retried(self):
        a=self.app;a.remote_mode=True;p=a.settings.pi_panel;f=Future();f.set_exception(OSError('offline'));p.job=f;p.job_kind='connect'
        p.poll();self.assertTrue(p.auto_enabled);self.assertTrue(p.recovery_pending);self.assertGreater(p.retry_at,time.monotonic())
    def test_snapshot_uses_saved_address_during_automatic_retry(self):
        a,p=self.broken();p.schedule_recovery();config={'host':'saved-pi','user':'robot','port':22,'identity_file':'','app_dir':'~/OpenCV_teach','python':'python3'};atomic_json(self.data/'pi-connection.json',config)
        a.settings.vars['pi_host'].set('unsaved-address')
        future=Future();future.set_exception(OSError('not started'))
        with patch.object(p.s.pool,'submit',return_value=future),patch('so101_teach.pi_connection.validate',wraps=__import__('so101_teach.pi_connection',fromlist=['validate']).validate) as validate:
            p.connect(automatic=True)
        self.assertEqual(validate.call_args.args[0]['host'],'saved-pi');p.job=None
