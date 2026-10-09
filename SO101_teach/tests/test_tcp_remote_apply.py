import unittest,time
from dataclasses import replace
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_remote as remote_fixtures,test_ui as ui_fixtures
from so101_teach.domain import Snapshot
from so101_teach.configuration import model_tcp

class RemoteTCPPresetTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    rpc=remote_fixtures.RemoteTests.rpc
    def connected(self):
        from so101_teach.motion import SPEED_PRESETS
        result=self.rpc('connect',{'speed':SPEED_PRESETS['보통']});self.assertTrue(result['ok'],result);s=self.runtime.session
        s.latest=Snapshot('follower',self.runtime.ref.middle,{n:{'torque':0} for n in self.runtime.cal.motors},time.monotonic(),time.time(),self.runtime.cal.sha256,True,'test')
        bundle=deepcopy(self.bundle);bundle['profile']['tcp']=model_tcp(point='center');return s,bundle
    def test_connected_torque_off_switches_tcp_without_motor_commands(self):
        s,bundle=self.connected();self.runtime.configure(bundle)
        self.assertIs(self.runtime.session,s);self.assertTrue(s.running);self.assertEqual(s.state,'READ_ONLY');self.assertTrue(s.commands.empty())
        self.assertEqual(self.runtime.profile['tcp']['model_point'],'center')
    def test_active_or_unverified_torque_blocks_tcp_change(self):
        s,bundle=self.connected();s.latest.telemetry['gripper']['torque']=1
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        self.assertNotEqual(self.runtime.profile['tcp'].get('model_point'),'center')
        s.latest.telemetry['gripper']['torque']=0;s.latest=replace(s.latest,monotonic=s.latest.monotonic-1)
        with self.assertRaises(ValueError):self.runtime.configure(bundle)

class UITCPRemoteTests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_apply_sends_selected_preset_before_saving_local_profile(self):
        a=self.app;s=a.settings;a.remote_mode=True;a.remote=SimpleNamespace(error=None,sync_bundle=Mock())
        try:
            s.vars['tcp_mode'].set('고정 집게 중앙');s.update_tcp_inputs();s.save_tcp()
            self.assertEqual(a.remote.sync_bundle.call_args.args[0]['profile']['tcp']['model_point'],'center');self.assertEqual(a.profile['tcp']['model_point'],'center')
        finally:a.remote_mode=False;a.remote=None
    def test_failed_pi_apply_does_not_change_local_tcp(self):
        a=self.app;s=a.settings;before=deepcopy(a.profile);a.remote_mode=True;a.remote=SimpleNamespace(error=None,sync_bundle=Mock(side_effect=ValueError('Pi rejected')))
        try:
            s.vars['tcp_mode'].set('고정 집게 중앙');s.update_tcp_inputs()
            with self.assertRaises(ValueError):s.save_tcp()
            self.assertEqual(a.profile,before)
        finally:a.remote_mode=False;a.remote=None
