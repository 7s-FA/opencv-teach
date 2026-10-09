"""Coordinate and configuration boundaries; no UI windows or physical devices."""
import base64,json,time,unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.domain import ROOT,Snapshot
from so101_teach.settings_ui import SettingsPanel
from so101_teach.height_reference import profile_with_arm_height
from tests import test_remote as remote_fixtures

class CameraCalculationLinkTests(unittest.TestCase):
    def setUp(self):
        profiles=json.loads((ROOT/'data/robot_profiles.json').read_text())
        p=deepcopy(next(p for p in profiles.values() if p.get('robot_id')=='arm3'))
        self.panel=SettingsPanel.__new__(SettingsPanel)
        self.app=SimpleNamespace(profile=p,remote_mode=False,remote=None,persist_configuration=Mock(),invalidate_jig_measurements=Mock())
        self.panel.app=self.app;self.panel.require_calculation_idle=Mock();self.panel.camera_info=SimpleNamespace(set=Mock())
    def test_restored_camera_is_in_selected_arm_frame_after_height_change(self):
        self.app.profile=profile_with_arm_height(self.app.profile,10)
        self.panel.restore_extrinsics()
        reference=json.loads((ROOT/'assets/reference/camera.json').read_text())['extrinsics']['base_from_camera']
        np.testing.assert_allclose(np.array(self.app.profile['world_from_base'])@np.array(self.app.profile['extrinsics']['base_from_camera']),reference,atol=1e-8)
    def test_camera_restore_is_sent_to_pi_before_local_commit(self):
        self.app.remote_mode=True;remote=SimpleNamespace(error=None,sync_bundle=Mock());self.app.remote=remote
        with patch('so101_teach.remote_config.configuration_bundle',return_value={'profile':deepcopy(self.app.profile)}):self.panel.restore_extrinsics()
        remote.sync_bundle.assert_called_once()
        self.assertEqual(remote.sync_bundle.call_args.args[0]['profile']['extrinsics'],self.app.profile['extrinsics'])
    def test_failed_pi_camera_apply_does_not_change_local_geometry(self):
        before=deepcopy(self.app.profile);self.app.remote_mode=True;self.app.remote=SimpleNamespace(error=None,sync_bundle=Mock(side_effect=ValueError('Pi rejected')))
        with patch('so101_teach.remote_config.configuration_bundle',return_value={'profile':deepcopy(self.app.profile)}):
            with self.assertRaises(ValueError):self.panel.restore_extrinsics()
        self.assertEqual(self.app.profile,before);self.app.persist_configuration.assert_not_called()
    def test_async_calibration_does_not_apply_to_another_profile(self):
        s=self.panel;s.board=lambda:(13,9,20);s.photo_paths=[];s.show_photos=Mock();s.run_job=Mock()
        s.calibrate_camera();done=s.run_job.call_args.args[1]
        self.app.profile['robot_id']='arm2';before=deepcopy(self.app.profile)
        result=deepcopy(self.app.profile['intrinsics']);result.update(source_files=[],errors_px=[],rms_px=.1)
        with self.assertRaises(ValueError):done(result)
        self.assertEqual(self.app.profile,before);self.app.persist_configuration.assert_not_called()

class RuntimeCalculationLinkTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    rpc=remote_fixtures.RemoteTests.rpc
    def connect_reader(self):
        from so101_teach.motion import SPEED_PRESETS
        self.assertTrue(self.rpc('connect',{'speed':SPEED_PRESETS['보통']})['ok'])
        s=self.runtime.session;s.latest=Snapshot('follower',self.runtime.ref.middle,{n:{'torque':0} for n in self.runtime.cal.motors},time.monotonic(),time.time(),self.runtime.cal.sha256,True,'test')
        return s
    def test_geometry_change_with_torque_hold_is_rejected(self):
        s=self.connect_reader();s.state='HOLD';before=deepcopy(self.runtime.profile)
        bundle=deepcopy(self.bundle);bundle['profile']['table_z_mm']-=5
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        self.assertEqual(self.runtime.profile,before);self.assertTrue(s.commands.empty())
    def test_new_angle_mapping_requires_reconnect_even_with_same_encoder_calibration(self):
        from tests.test_angle_calibration import points
        s=self.connect_reader();bundle=deepcopy(self.bundle)
        name=bundle['profile']['calibration_file'].replace('.json','.angles.json')
        bundle['files'][name]=base64.b64encode(json.dumps(points(self.runtime.cal)).encode()).decode()
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        self.assertIsNone(s.calibration.angle_mapping)
    def test_fresh_reader_applies_geometry_and_trims_without_motion(self):
        s=self.connect_reader();bundle=deepcopy(self.bundle)
        bundle['profile']=profile_with_arm_height(bundle['profile'],10);bundle['trim_ticks']['wrist_roll']=7
        self.runtime.configure(bundle)
        self.assertEqual(self.runtime.profile['extrinsics'],bundle['profile']['extrinsics'])
        self.assertEqual(self.runtime.ref.trims['wrist_roll'],7);self.assertTrue(s.commands.empty());self.assertEqual(s.state,'READ_ONLY')
    def test_stale_or_torque_on_sample_rejects_geometry_change(self):
        from dataclasses import replace
        s=self.connect_reader();bundle=deepcopy(self.bundle);bundle['profile']['table_z_mm']-=5
        valid=s.latest;s.latest=replace(valid,monotonic=valid.monotonic-1)
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        s.latest=valid;s.latest.telemetry['wrist_roll']['torque']=1
        with self.assertRaises(ValueError):self.runtime.configure(bundle)

from tests import test_ui as ui_fixtures
class ModelPersistenceLinkTests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_model_trim_survives_arm_switch_and_reload(self):
        import tkinter as tk
        from so101_teach.domain import read_json
        from so101_teach.ui import App
        a=self.app;a.trim_vars['wrist_roll'].set(7);a.save_model();saved=deepcopy(a.profile);fk=a.kin.fk(a.target)
        other=deepcopy(saved);other.update(robot_id='arm3',name='temporary arm3');other['model_reference'].pop('trim_ticks')
        a.install_profile(other);a.install_profile(saved)
        self.assertEqual(a.reference.trims['wrist_roll'],7);np.testing.assert_allclose(a.kin.fk(a.target),fk)
        a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.reference.trims['wrist_roll'],7)
        self.assertEqual(read_json(self.data/'profile.json')['model_reference']['trim_ticks']['wrist_roll'],7)
    def test_real_remote_detector_failure_keeps_camera_settings_and_file(self):
        from so101_teach.remote_client import RemoteDetector
        a=self.app;old_detector=a.detector;before=deepcopy(a.profile);raw=(self.data/'profile.json').read_bytes()
        link=SimpleNamespace(error=None,sync_bundle=Mock(side_effect=ValueError('Pi rejected')),sync=Mock(side_effect=ValueError('Pi rejected')),rpc=Mock())
        a.remote_mode=True;a.remote=link;a.detector=RemoteDetector(a,link)
        try:
            with self.assertRaises(ValueError):a.settings.restore_extrinsics()
            self.assertEqual(a.profile,before);self.assertEqual((self.data/'profile.json').read_bytes(),raw)
            link.sync.assert_not_called()
        finally:a.remote_mode=False;a.remote=None;a.detector=old_detector

if __name__=='__main__':unittest.main()
