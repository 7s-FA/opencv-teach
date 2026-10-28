import unittest
from unittest.mock import patch
from tests import test_ui as fixtures


class StartupConnectionTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def test_startup_and_stale_callback_never_open_local_or_pi_devices(self):
        a=self.app
        with patch.object(a.settings.pi_panel,'connect') as pi,patch.object(a,'connect_devices') as motors:
            for remote in (False,True):
                for mode in ('follower','leader'):
                    a.remote_mode=remote;a.profile['mode']=mode;a.startup_devices_pending=True
                    a.startup_connections();a.connect_startup_devices()
                    self.assertFalse(a.startup_devices_pending)
        pi.assert_not_called();motors.assert_not_called();self.assertIsNone(a.session)
        self.assertIn('조회 전용',a.connection.get())

    def test_device_connection_requires_explicit_all_connect_request(self):
        self.app.profile['mode']='follower'
        with patch('so101_teach.ui.MotionSession.start') as start:
            with self.assertRaisesRegex(ValueError,'전체 연결'):self.app.connect_devices()
            start.assert_not_called()
            self.app.connect_devices(control_requested=True)
            start.assert_called_once()

    def test_pi_selection_and_automatic_callbacks_never_acquire(self):
        a=self.app;p=a.settings.pi_panel
        with patch('so101_teach.remote_client.RemoteLink.open') as open_link,patch.object(a,'start_linear_state_query'):
            p.use_pi();p.connect(automatic=True)
            with self.assertRaisesRegex(ValueError,'전체 연결'):p.connect()
            open_link.assert_not_called()
        self.assertTrue(a.remote_mode);self.assertIsNone(a.remote)

    def test_closed_or_demo_startup_never_connects(self):
        a=self.app
        with patch.object(a,'connect_devices') as connect:
            a.profile['mode']='demo';a.startup_connections()
            a.closed=True;a.startup_connections();a.closed=False
        connect.assert_not_called()
