import unittest,time
from types import SimpleNamespace
from unittest.mock import Mock,patch
from concurrent.futures import Future
from tests import test_ui as fixtures

class RemoteUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def link(self):
        a=self.app;a.remote_mode=True;a.remote=SimpleNamespace(error=None,bundle_hash='config',rpc=Mock(),sync=Mock(),close=Mock())
        return a.remote
    def test_remote_selection_never_falls_back_to_local_camera_or_motors(self):
        a=self.app;a.remote_mode=True
        with patch('so101_teach.ui.CameraSession.start',side_effect=AssertionError('local camera')),patch('so101_teach.ui.MotionSession.start',side_effect=AssertionError('local motor')):
            with self.assertRaisesRegex(ValueError,'Pi 연결'):a.start_camera()
            with self.assertRaisesRegex(ValueError,'Pi 연결'):a.toggle_connection()
    def test_remote_plan_runs_off_ui_thread_and_dispatches_exact_result(self):
        a=self.app;link=self.link();step=a.store.step(a.target,'following');step['jig_id']='test-jig';step['jig_reference']={'pose':[0,0,0],'symmetry_deg':180};plan=[{'ticks':step['ticks'],'corrected':False}]
        link.rpc.return_value={'plan':plan,'calibration_sha256':a.calibration.sha256,'bundle_hash':'config'}
        with patch.object(a,'motion_request',return_value=9) as move:
            a.begin_execution('play',[step],None);self.assertIsNotNone(a.remote_plan_job);move.assert_not_called()
            end=time.monotonic()+2
            while a.remote_plan_job and time.monotonic()<end:a.poll_remote_plan();time.sleep(.01)
            move.assert_called_once_with('play',[step['ticks']])
        link.rpc.assert_called_once();self.assertEqual(link.rpc.call_args.args[0],'plan')
    def test_stopping_during_remote_ik_never_dispatches_late_result(self):
        a=self.app;self.link();future=Future();future.set_running_or_notify_cancel();step=a.store.step(a.target,'fixed')
        a.remote_plan_job=(future,('play',[step],None),a.episode['id'])
        with patch.object(a,'motion_request') as move:
            a.stop_preview();future.set_result({'plan':[{'ticks':step['ticks'],'corrected':False}]});a.poll_remote_plan();move.assert_not_called()
        self.assertIsNone(a.remote_plan_job)
    def test_episode_saves_stay_local_until_explicit_export(self):
        a=self.app;link=self.link();ticks=a.target.copy();a.commit_target()
        calls=[c for c in link.rpc.call_args_list if c.args[0]=='save_episode'];self.assertFalse(calls)
        self.assertEqual(a.store.load(a.store.directory/(a.episode['id']+'.json'))['steps'][0]['ticks'],ticks)
    def test_remote_mode_leader_uses_pc_read_only_session(self):
        from so101_teach.devices import ReadOnlySession
        from so101_teach.remote_client import RemoteLeaderSession
        a=self.app;link=self.link();a.session=SimpleNamespace(running=False,close=Mock())
        config={'port':'/dev/pc-leader'}
        with patch.object(ReadOnlySession,'start') as start:
            a.start_leader_connection(config,a.calibration)
        self.assertIsInstance(a.leader_session,RemoteLeaderSession);start.assert_called_once()
        self.assertIs(link.leader_client,a.leader_session);self.assertEqual(a.leader_session.port,'/dev/pc-leader')
    def test_remote_mode_leader_calibration_stays_local(self):
        a=self.app;link=self.link();s=a.settings;s.vars['cal_role'].set('리더');s.vars['cal_port'].set('/dev/pc-leader');s.vars['leader_port'].set('/dev/pc-leader')
        with patch('so101_teach.calibration.CalibrationWorker') as local,patch('so101_teach.remote_client.RemoteCalibrationWorker') as remote:
            local.return_value.running=False;s.start_calibration()
        local.assert_called_once();remote.assert_not_called();self.assertEqual(local.call_args.args[0],'/dev/pc-leader')
