import json,tempfile,time,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from so101_teach.domain import ROOT,load_profile
from so101_teach.configuration import JigCatalog
from so101_teach.geometry import Kinematics,corrected_step,corrected_plan,jig_delta
from so101_teach.jig_compatibility import execution_reference,LEGACY_CARRIER_SHA,ASSEMBLED_CARRIER_SHA
from so101_teach.jig_heading import pose_in_reference_basis
from so101_teach.jig_comparison import comparison_groups
from so101_teach.jig_consensus import StableCandidate
from so101_teach.vision import PoseLatch
from so101_teach.camera_lifecycle import CameraLifecycle
from tests.test_pose_hold import result

class CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from so101_teach.domain import Calibration,ModelReference
        profiles=json.loads((ROOT/'data/robot_profiles.json').read_text())
        cls.profile=next(p for p in profiles.values() if p.get('robot_id')=='arm2')
        cls.cal=Calibration(ROOT/'data'/cls.profile['calibration_file']);cls.ref=ModelReference(cls.cal,cls.profile['model_reference'])
        cls.catalog=JigCatalog(ROOT/'data',profile=cls.profile);cls.kin=Kinematics(cls.ref,tcp=cls.profile.get('tcp'))
        from tests.fixtures import legacy_carrier_episode
        cls.episode=legacy_carrier_episode();cls.steps=[s for s in cls.episode['steps'] if s.get('jig_id') not in (None,'pallet')]
    def current(self,step):return {'pose':deepcopy(step['jig_reference']['pose']),'symmetry_deg':360,'stl_sha256':ASSEMBLED_CARRIER_SHA,'mesh_yaw_offset_deg':180.}
    def test_all_twelve_saved_carrier_steps_keep_exact_ticks_at_the_same_pose(self):
        before=deepcopy(self.episode);self.assertEqual(len(self.steps),12)
        for step in self.steps:
            current=self.current(step);out=corrected_step(self.kin,step,current,ASSEMBLED_CARRIER_SHA)
            self.assertEqual(out['ticks'],step['ticks']);self.assertEqual(out['position_error_mm'],0.)
            self.assertEqual(out['jig_reference_compatibility'],'legacy_carrier_to_assembly')
        self.assertEqual(self.episode,before)
    def test_only_verified_models_and_known_legacy_basis_are_compatible(self):
        step=deepcopy(self.steps[0]);current=self.current(step)
        for mode in ('wrong_registered','wrong_saved','wrong_measured','old_measured_basis','unknown_saved_basis'):
            s=deepcopy(step);c=deepcopy(current);sha=ASSEMBLED_CARRIER_SHA
            if mode=='wrong_registered':sha='c'*64
            elif mode=='wrong_saved':s['jig_reference']['stl_sha256']='d'*64
            elif mode=='wrong_measured':c['stl_sha256']='e'*64
            elif mode=='old_measured_basis':c.pop('mesh_yaw_offset_deg')
            else:s['jig_reference']['symmetry_deg']=90
            with self.assertRaises(ValueError,msg=mode):corrected_step(self.kin,s,c,sha)
        step['jig_reference']['stl_sha256']=ASSEMBLED_CARRIER_SHA
        self.assertEqual(corrected_step(self.kin,step,current,ASSEMBLED_CARRIER_SHA)['ticks'],step['ticks'])
    def test_true_half_turn_and_comparison_use_the_same_upgraded_basis(self):
        step=self.steps[0];c=self.current(step);c['pose'][2]+=180
        reference=execution_reference(step['jig_reference'],c,ASSEMBLED_CARRIER_SHA)
        adjusted=jig_delta(reference['pose'],pose_in_reference_basis(reference,c),reference['symmetry_deg'])
        self.assertAlmostEqual(abs(adjusted[2]-reference['pose'][2]),180)
        group=comparison_groups([step],self.catalog.items,{step['jig_id']:c})[0]
        self.assertAlmostEqual(abs(group['delta'][2]),180);self.assertEqual(group['reference'],step['jig_reference']);self.assertEqual(group['execution_reference'],reference)

    def test_b_upper_update_preserves_previous_assembly_pose_and_rejects_unknown_basis(self):
        from so101_teach.jig_compatibility import PREVIOUS_ASSEMBLED_CARRIER_SHA
        step=deepcopy(self.steps[0]);current=self.current(step)
        step['jig_reference']={**current,'stl_sha256':PREVIOUS_ASSEMBLED_CARRIER_SHA}
        before=deepcopy(step)
        out=corrected_step(self.kin,step,current,ASSEMBLED_CARRIER_SHA)
        self.assertEqual(out['ticks'],step['ticks']);self.assertEqual(out['jig_reference_compatibility'],'carrier_b_top_update')
        self.assertEqual(step,before)
        for changes in ({'mesh_yaw_offset_deg':0},{'stl_sha256':'f'*64},{'symmetry_deg':90}):
            invalid=deepcopy(step);invalid['jig_reference'].update(changes)
            with self.assertRaises(ValueError):corrected_step(self.kin,invalid,current,ASSEMBLED_CARRIER_SHA)
    def test_full_real_episode_plan_matches_pc_and_pi_without_device_access(self):
        from so101_teach.remote_config import configuration_bundle
        from so101_teach.remote_server import Runtime
        current={}
        for step in self.episode['steps']:
            key=step.get('jig_id')
            if key and key not in current:current[key]=self.current(step) if key!='pallet' else deepcopy(step['jig_reference'])
        before=deepcopy(self.episode);pc=corrected_plan(self.kin,self.episode['steps'],current,lambda k:self.catalog.mesh(k)['sha256'])
        app=SimpleNamespace(profile=self.profile,data_dir=ROOT/'data',catalog=self.catalog,reference=self.ref,pose_latch=PoseLatch(),active_jig='pallet')
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Runtime(tmp)
            try:
                runtime.configure(configuration_bundle(app));pi=runtime.call('plan',{'steps':self.episode['steps'],'current':current})['plan']
                self.assertIsNone(runtime.session);self.assertIsNone(runtime.camera)
            finally:runtime.close()
        self.assertEqual([r['ticks'] for r in pc],[r['ticks'] for r in pi]);self.assertEqual(self.episode,before)
        self.assertEqual(sum(r.get('jig_reference_compatibility')=='legacy_carrier_to_assembly' for r in pc),12)

class TimingAndFailureTests(unittest.TestCase):
    def test_late_poll_does_not_extend_the_second_attempt(self):
        s=StableCandidate(3,2);s.clear(0);s.update(result(strong=True),.5);s.finish(3.2)
        s.update(result(strong=True),4);s.update(result(strong=True),5);out=s.finish(6)
        self.assertIsNotNone(out['selected']);self.assertEqual(out['acquisition_completed_at'],6);self.assertEqual(out['acquisition_completed_attempts'],2)
    def test_long_stall_exhausts_empty_windows_without_reviving_old_pose(self):
        latch=PoseLatch();latch.update(result(strong=True),0);latch.update(result(strong=True),1)
        out=latch.poll(100);self.assertIsNone(out['selected']);self.assertIsNone(out['pose_measured_at'])
        self.assertTrue(out['acquisition_exhausted']);self.assertIsNone(latch.update(result(strong=True),101)['selected'])
    def test_unfreeze_starts_new_acquisition_instead_of_expiring_during_the_frozen_period(self):
        latch=PoseLatch()
        for t in (0,1,2,3):latch.update(result(strong=True),t)
        latch.freeze(True);latch.poll(100);latch.freeze(False)
        for t in (101,102,103):self.assertIsNone(latch.update(result(130,strong=True),t)['selected'])
        self.assertIsNotNone(latch.poll(104)['selected'])
    def app(self):
        raw=result(strong=True);raw['pose_measured_at']=99.9
        return SimpleNamespace(camera=SimpleNamespace(observation=(np.zeros((2,2,3),np.uint8),raw,99.9),error=None,recovering=False),camera_sleeping=False,active_jig='pallet',pose_latch=SimpleNamespace(seconds=10),measurement_valid_after=0.,teaching_jig_hold_active=lambda:False)
    def test_recent_image_cannot_hide_camera_error_or_invalid_measurement_age(self):
        for kind in ('error','recovering','expired','future','invalidated'):
            a=self.app()
            if kind=='error':a.camera.error='disconnected'
            elif kind=='recovering':a.camera.recovering=True
            elif kind=='expired':a.camera.observation[1]['pose_measured_at']=89.
            elif kind=='future':a.camera.observation[1]['pose_measured_at']=101.
            else:a.measurement_valid_after=100.
            self.assertEqual(CameraLifecycle.camera_results(a,100),{},kind)
