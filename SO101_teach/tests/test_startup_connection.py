import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests import test_ui as fixtures


class StartupConnectionTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def prepare(self,mode='leader'):
        a=self.app;a.profile['mode']=mode;a.startup_devices_pending=True
        a.profile['leader']={'port':'/dev/test-leader','calibration_file':a.profile['calibration_file']}
        return a

    def test_local_startup_connects_both_read_only_once(self):
        a=self.prepare()
        with patch('so101_teach.ui.MotionSession.start') as follower,patch('so101_teach.ui.ReadOnlySession.start') as leader,patch('so101_teach.ui.MotionSession.request',side_effect=AssertionError('No motion at startup')):
            a.startup_connections();a.connect_startup_devices()
        follower.assert_called_once();leader.assert_called_once()
        self.assertEqual(a.session.state,'READ_ONLY');self.assertEqual(a.leader_session.port,'/dev/test-leader')
        self.assertFalse(a.startup_devices_pending)

    def test_follower_mode_never_opens_leader(self):
        a=self.prepare('follower')
        with patch('so101_teach.ui.MotionSession.start') as follower,patch.object(a,'start_leader_connection',side_effect=AssertionError('No leader')):
            a.startup_connections()
        follower.assert_called_once();self.assertIsNone(a.leader_session)

    def test_demo_and_closed_windows_do_not_connect(self):
        a=self.prepare('demo')
        with patch.object(a,'connect_devices') as connect,patch.object(a,'start_camera') as camera:
            a.startup_connections();a.profile['mode']='leader';a.closed=True
            try:a.startup_connections();a.connect_startup_devices()
            finally:a.closed=False
        connect.assert_not_called();camera.assert_not_called()

    def test_camera_failure_does_not_prevent_motor_connection(self):
        a=self.prepare('follower');a.camera_auto_view=True
        with patch.object(a,'start_camera',side_effect=OSError('Camera missing')),patch('so101_teach.ui.MotionSession.start') as start:
            a.startup_connections()
        start.assert_called_once()

    def test_pi_startup_waits_for_connection_without_local_fallback(self):
        a=self.prepare();a.remote_mode=True
        with patch.object(a.settings.pi_panel,'connect') as pi,patch.object(a,'connect_devices',side_effect=AssertionError('Pi not ready')),patch.object(a,'start_camera',side_effect=AssertionError('No local camera')):
            a.startup_connections()
        pi.assert_called_once();self.assertTrue(a.startup_devices_pending)

    def test_existing_pi_session_is_adopted_and_pc_leader_connected(self):
        a=self.prepare();a.remote_mode=True
        follower={'running':True,'state':'HOLD','error':None,'index':0,'request_id':3,'completed_request_id':3,'gripper_hold_tick':None,'program_active':False,'command_pending':False,'latest':None}
        link=SimpleNamespace(error=None,latest_state={'follower':follower,'server_now':time.monotonic()},listeners={},config={'host':'pi'},health={'devices':{'serial':['/dev/pi-follower'],'cameras':[]}},rpc=Mock(),close=Mock())
        with patch('so101_teach.remote_client.RemoteDetector',return_value=SimpleNamespace(latches={a.active_jig:a.pose_latch})),patch.object(a.settings,'refresh_device_ports'),patch.object(a,'start_camera') as camera,patch.object(a,'start_leader_connection') as leader:
            a.settings.pi_panel.install(link)
        camera.assert_not_called();leader.assert_called_once();link.rpc.assert_called_once_with('camera_stop')
        self.assertEqual(a.session.state,'HOLD');self.assertFalse(a.startup_devices_pending)
        a.session.running=False

    def test_fresh_pi_startup_sends_connect_not_motion(self):
        a=self.prepare();a.remote_mode=True
        link=SimpleNamespace(error=None,listeners={},sync=Mock(),rpc=Mock(),close=Mock());a.remote=link
        with patch.object(a,'start_leader_connection') as leader:
            a.connect_startup_devices()
        self.assertEqual([c.args[0] for c in link.rpc.call_args_list],['connect'])
        leader.assert_called_once();self.assertEqual(a.session.state,'READ_ONLY')
        a.session.running=False

    def test_failed_startup_remains_manual_retry_without_loop(self):
        a=self.prepare()
        with patch.object(a,'connect_devices',side_effect=OSError('USB missing')) as connect:
            a.connect_startup_devices();a.connect_startup_devices()
        connect.assert_called_once();self.assertFalse(a.startup_devices_pending)


if __name__=='__main__':unittest.main()
