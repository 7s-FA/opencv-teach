"""Unconnected/read-only windows close; park only the connected arms."""
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
from tests import test_arm_workspaces as fixture
from so101_teach.safe_shutdown import SafeShutdown


class PartialShutdownTests(unittest.TestCase):
    setUp=fixture.ArmWorkspacesTests.setUp
    tearDown=fixture.ArmWorkspacesTests.tearDown

    def remote(self,app):
        app.remote_mode=True
        app.remote=NS(error=None,stop=threading.Event(),lease='lease',token='token',instance=app.profile['robot_id'],
            server_port=8765,config=dict(host='test',port=22,user='test',app_dir='/app'),close=Mock(),relinquish=Mock())
        return app.remote

    def test_pi_link_without_any_follower_does_not_require_two_arms_to_close(self):
        self.remote(self.a)
        with patch('so101_teach.safe_shutdown.SafeShutdown.start') as park:
            self.manager.close();park.assert_not_called()
        self.assertTrue(self.manager.closing)

    def test_one_read_only_arm_closes_without_safe_pose_or_motion(self):
        self.remote(self.a);session=fixture.ArmWorkspacesTests.connected(self,self.a,'READ_ONLY')
        self.a.episode['steps']=[]
        with patch('so101_teach.safe_shutdown.SafeShutdown.start') as park:
            self.manager.close();park.assert_not_called()
        self.assertTrue(self.manager.closing);session.request.assert_not_called()

    def test_both_read_only_arms_close_without_turning_torque_on(self):
        for app in (self.a,self.b):
            self.remote(app);fixture.ArmWorkspacesTests.connected(self,app,'READ_ONLY')
        with patch('so101_teach.safe_shutdown.SafeShutdown.start') as park:
            self.manager.close();park.assert_not_called()
        self.assertTrue(self.manager.closing)
        for app in (self.a,self.b):app.session.request.assert_not_called()

    def test_single_connected_arm_request_omits_disconnected_peer(self):
        self.remote(self.b);fixture.ArmWorkspacesTests.connected(self,self.b)
        job=SafeShutdown(self.manager)
        config,request=job.request()
        self.assertEqual(set(request['arms']),{'arm3'});self.assertEqual(set(request['links']),{'arm3'})
        self.assertEqual(request['first'],'arm3')
        job.thread=object();job.events.put(('line','SHUTDOWN_ACCEPTED:job'))
        with patch.object(self.manager,'finish_safe_close') as finish:
            job.poll();finish.assert_called_once()
        self.b.remote.relinquish.assert_called_once()

    def test_stale_or_incomplete_torque_data_does_not_skip_safe_shutdown(self):
        session=fixture.ArmWorkspacesTests.connected(self,self.a,'READ_ONLY')
        session.latest.telemetry.clear()
        with patch('so101_teach.safe_shutdown.SafeShutdown.start') as park:
            self.manager.close();park.assert_called_once()
        self.assertFalse(self.manager.closing)

    def test_single_arm_validation_is_available_only_to_explicit_shutdown_scope(self):
        import tempfile
        from pathlib import Path
        from so101_teach.remote_config import materialize
        from so101_teach.pi_execution_receiver import validate_snapshot
        self.remote(self.b);fixture.ArmWorkspacesTests.connected(self,self.b)
        _,request=SafeShutdown(self.manager).request()
        part={**request,**request['arms']['arm3'],'arm':'arm3'}
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);materialize(root/'data/arm3-runtime',part['bundle'])
            cli=NS(ROOT=root,INSPECTION_PROTOCOL=6)
            with self.assertRaisesRegex(ValueError,'연결이 누락'):
                validate_snapshot(cli,part)
            _,episode,_,_,_=validate_snapshot(cli,part,required_arms=('arm3',))
            self.assertEqual(episode['robot_id'],'arm3')

    def test_empty_shutdown_scope_is_rejected_before_opening_devices(self):
        from tests import test_episode_cli  # Install the CLI module search path.
        from so101_teach.shutdown_receiver import park
        with self.assertRaisesRegex(ValueError,'일치하지'):
            park(NS(),{'arms':{},'links':{}},None,Mock(),check=True)
