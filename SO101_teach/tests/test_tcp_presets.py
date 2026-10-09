import unittest,tkinter as tk
from copy import deepcopy
import numpy as np
from tests import test_ui as fixtures
from so101_teach.configuration import model_tcp,tcp_label
from so101_teach.domain import read_json
from so101_teach.ui import App
from so101_teach.inspection import build_scene
from so101_teach.remote_config import configuration_bundle

class TCPPresetTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_tip_stays_unchanged_and_center_is_existing_model_origin(self):
        np.testing.assert_allclose(model_tcp()['xyz_mm'],[1.773409675938293,.6026567343814877,6.549653899876567])
        self.assertEqual(model_tcp(point='center')['xyz_mm'],[0.,0.,0.])
        self.assertEqual(tcp_label(model_tcp()),'고정 집게 끝단');self.assertEqual(tcp_label(model_tcp(point='center')),'고정 집게 중앙')
        with self.assertRaises(ValueError):model_tcp(point='unexpected')
    def test_selection_previews_only_and_model_fields_are_readonly(self):
        a=self.app;s=a.settings;profile=deepcopy(a.profile)
        s.vars['tcp_mode'].set('고정 집게 중앙');s.update_tcp_inputs()
        self.assertEqual(s.model_spec('tcp')['tcp']['xyz_mm'],[0.,0.,0.]);self.assertEqual(a.profile,profile)
        self.assertTrue(all(str(f['state'])=='readonly' for f in s.tcp_fields))
        s.vars['tcp_mode'].set('직접 설정');s.update_tcp_inputs();self.assertTrue(all(str(f['state'])=='normal' for f in s.tcp_fields))
    def test_save_changes_kinematics_and_bundle_but_not_episode_or_calibration(self):
        a=self.app;s=a.settings;a.commit_target();episode=deepcopy(a.episode);cal=(self.data/'calibration/follower.json').read_bytes()
        old=a.kin.fk(a.target).copy();s.vars['tcp_mode'].set('고정 집게 중앙');s.update_tcp_inputs();s.save_tcp()
        self.assertFalse(np.allclose(a.kin.fk(a.target),old));self.assertEqual(a.episode,episode);self.assertEqual((self.data/'calibration/follower.json').read_bytes(),cal)
        self.assertEqual(configuration_bundle(a)['profile']['tcp']['model_point'],'center')
        self.assertEqual(read_json(self.data/'profile.json')['tcp']['xyz_mm'],[0.,0.,0.])
        s.load_model_tcp();s.save_tcp();np.testing.assert_allclose(a.kin.fk(a.target),old)
    def test_center_survives_restart_and_profile_install(self):
        a=self.app;s=a.settings;s.vars['tcp_mode'].set('고정 집게 중앙');s.update_tcp_inputs();s.save_tcp();profile=deepcopy(a.profile)
        a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.settings.value('tcp_mode'),'고정 집게 중앙');self.assertEqual(self.app.profile['tcp']['xyz_mm'],[0.,0.,0.])
        self.app.install_profile(profile);self.assertEqual(self.app.profile['tcp']['model_point'],'center')
    def test_legacy_model_profile_keeps_tip(self):
        p=deepcopy(self.app.profile);p['tcp'].pop('model_point',None);self.app.install_profile(p)
        self.assertEqual(self.app.settings.value('tcp_mode'),'고정 집게 끝단')
    def test_center_preview_marker_coincides_with_model_origin(self):
        _,_,markers,info=build_scene({'kind':'tcp','tcp':model_tcp(point='center')})
        np.testing.assert_allclose(markers['tcp'],markers['origin']);self.assertEqual(info['offset_mm'],0.)
        self.assertEqual(info['tcp_label'],'고정 집게 중앙')
