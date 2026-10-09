import unittest,tempfile
from copy import deepcopy
import numpy as np
from so101_teach.domain import ROOT,read_json,EpisodeStore
from tests.fixtures import load_profile
from so101_teach.geometry import Kinematics,corrected_step,jig_delta,transform_jig_pose

class JigStepTests(unittest.TestCase):
    def setUp(self):
        _,self.cal,self.ref=load_profile();self.kin=Kinematics(self.ref)
        self.ticks=read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks']
        self.reference={'pose':[228,-138,67],'symmetry_deg':90,'stl_sha256':'a'*64}
        self.step={'id':'step','name':'잡기','ticks':self.ticks.copy(),'jig_id':'pallet','jig_reference':deepcopy(self.reference)}
    def test_identity_and_fixed_step_preserve_all_raw_ticks_exactly(self):
        self.assertEqual(corrected_step(self.kin,self.step,self.reference,'a'*64)['ticks'],self.ticks)
        fixed={**self.step,'jig_id':None}
        self.assertEqual(corrected_step(self.kin,fixed,None,'other')['ticks'],self.ticks)
    def test_translation_rotation_preserves_taught_height_and_gripper(self):
        before=deepcopy(self.step)
        for pose in ([233,-138,70],[238,-133,72],[250,-120,72]):
            current={**self.reference,'pose':pose};out=corrected_step(self.kin,self.step,current,'a'*64)
            target=transform_jig_pose(self.kin.fk(self.ticks),self.reference['pose'],pose)
            np.testing.assert_allclose(self.kin.fk(out['ticks'])[:3,3],target[:3,3],atol=.8)
            self.assertEqual(out['ticks']['gripper'],self.ticks['gripper']);self.assertLess(out['orientation_error_deg'],5)
        self.assertEqual(self.step,before)
    def test_nearest_symmetric_angle_avoids_ninety_degree_flip(self):
        np.testing.assert_allclose(jig_delta([1,2,89],[3,4,1],90),[3,4,91])
        np.testing.assert_allclose(jig_delta([1,2,179],[3,4,1],180),[3,4,181])
    def test_missing_wrong_fixture_or_unreachable_target_do_not_fallback_to_fixed(self):
        for current,sha in [(None,'a'*64),(self.reference,'b'*64),({**self.reference,'pose':[10000,10000,67]},'a'*64)]:
            with self.assertRaises(ValueError):corrected_step(self.kin,self.step,current,sha)
    def test_step_reference_roundtrip_and_incomplete_reference_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            store=EpisodeStore(d,self.cal);ep=store.new();ep['steps']=[self.step]
            self.assertEqual(store.load(store.save(ep))['steps'][0]['jig_reference'],self.reference)
            del ep['steps'][0]['jig_reference']
            with self.assertRaises(ValueError):store.save(ep)
