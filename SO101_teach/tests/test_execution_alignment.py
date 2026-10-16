"""Offline trial mapping contracts and original-formula runtime parity, without device I/O."""
import json,tempfile,unittest,hashlib
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from so101_teach.domain import ROOT,JOINTS,read_json,load_profile,Calibration,ModelReference
from so101_teach.joint_alignment import ExecutionKinematics,SimulationMapping,load_alignment,CONFIG_DIR,CONFIG_NAMES
from so101_teach.geometry import Kinematics,corrected_step,corrected_plan
from so101_teach.remote_server import Runtime
from so101_teach.remote_config import configuration_bundle
from so101_teach.configuration import JigCatalog
from so101_teach.vision import PoseLatch
from so101_teach import runtime_version

class ExecutionAlignmentTests(unittest.TestCase):
    def inputs(self,arm):
        profiles=read_json(ROOT/'data/robot_profiles.json')
        profile=next(p for p in profiles.values() if p.get('robot_id','arm2')==arm)
        _,cal,ref=load_profile(ROOT/'data',profile=profile)
        eid={'arm2':'dea969d731e8449494285c4fbc8ca066','arm3':'8851f8f6f68343588131edf685bfbf30'}[arm]
        episode=read_json(ROOT.parent/'SO101_rl/config/reference_episodes'/f'{eid}.json')
        return profile,cal,ref,episode
    def test_both_arms_use_exact_frozen_simulation_mapping_and_inverse(self):
        for arm in CONFIG_NAMES:
            profile,cal,ref,episode=self.inputs(arm);before=deepcopy(profile)
            kin=ExecutionKinematics(profile,ref);kin.require_ready();self.assertIsInstance(kin.reference,SimulationMapping)
            source=read_json(ROOT.parent/'SO101_rl/config'/CONFIG_NAMES[arm])
            expected=SimulationMapping(ref,source['offset_deg'],source['gains_negative'],source['gains_positive'],source['correction_knots_deg'])
            for step in episode['steps']:
                ticks=step['ticks'];q=kin.reference.angles(ticks)
                np.testing.assert_array_equal(q,expected.angles(ticks));self.assertEqual(kin.reference.ticks_from_angles(q),ticks)
                np.testing.assert_array_equal(kin.fk(ticks),kin.fk_angles(q))
            self.assertEqual(profile,before);self.assertEqual(ref.radians,profile['model_reference']['radians'])
    def test_fixed_steps_preserve_ticks_and_gripper(self):
        for arm in CONFIG_NAMES:
            p,cal,ref,ep=self.inputs(arm);kin=ExecutionKinematics(p,ref)
            for step in ep['steps']:
                if step.get('jig_id'):continue
                r=step.get('jig_reference');out=corrected_step(kin,step,r,r['stl_sha256'] if r else '')
                self.assertEqual(out['ticks'],step['ticks'])
    def test_mismatch_missing_or_disabled_map_refuses_ik_without_old_formula_fallback(self):
        p,cal,ref,ep=self.inputs('arm2');step=next(s for s in ep['steps'] if s.get('jig_id'));r=step['jig_reference']
        source=read_json(CONFIG_DIR/CONFIG_NAMES['arm2'])
        with tempfile.TemporaryDirectory() as folder:
            for key,value in [('missing',None),('robot_id','arm3'),('source_model_reference',{}),('execution_trial_enabled',False),('calibration_sha256','wrong')]:
                if key!='missing':
                    d=deepcopy(source);d[key]=value;(Path(folder)/CONFIG_NAMES['arm2']).write_text(json.dumps(d))
                kin=ExecutionKinematics(p,ref,config_dir=folder)
                with self.assertRaisesRegex(ValueError,'실행 보정 계산 중단'):corrected_step(kin,step,r,r['stl_sha256'])
                # Raw-angle drawing and unchanged fixed ticks do not run IK.
                self.assertTrue(np.isfinite(kin.fk_angles(ref.angles(ref.middle))).all())
    def test_remote_runtime_and_local_app_engine_produce_identical_changed_jig_ticks(self):
        for arm in CONFIG_NAMES:
            p,cal,ref,ep=self.inputs(arm);kin=Kinematics(ref,tcp=p['tcp']);app=SimpleNamespace(profile=p,data_dir=ROOT/'data',reference=ref,catalog=JigCatalog(ROOT/'data',profile=p),pose_latch=PoseLatch(),active_jig=next(iter(read_json(ROOT/'data/jigs.json'))))
            selected=[s for s in ep['steps'] if s.get('jig_id')];step=deepcopy(selected[len(selected)//2]);key=step['jig_id'];now=deepcopy(step['jig_reference']);now['pose'][0]+=1.;now['pose'][1]+=1.;now['pose'][2]+=.2
            now['stl_sha256']=app.catalog.mesh(key)['sha256']
            with tempfile.TemporaryDirectory() as folder,patch('so101_teach.devices.new_bus',side_effect=AssertionError('device access prohibited')):
                runtime=Runtime(folder,robot_id=arm)
                try:
                    runtime.configure(configuration_bundle(app));self.assertIs(type(runtime.kin),Kinematics);self.assertIs(runtime.kin.reference,runtime.ref)
                    a=corrected_plan(kin,[step],{key:now},lambda k:now['stl_sha256']);b=runtime.call('plan',{'steps':[step],'current':{key:now}})['plan']
                    self.assertEqual(a,b);cal.ticks(a[0]['ticks']);self.assertEqual(a[0]['ticks']['gripper'],step['ticks']['gripper']);self.assertIsNone(runtime.session)
                finally:runtime.close()
    def test_runtime_identity_includes_trial_assets_and_missing_asset_is_diffable(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'so101_teach').mkdir()
            for name in runtime_version.REQUIRED_ASSETS:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
            before=runtime_version.manifest(root);name='assets/joint_alignment/joint_alignment.json';(root/name).unlink()
            self.assertIn(name,runtime_version.difference(before,runtime_version.manifest(root,allow_missing=True)))
            with self.assertRaisesRegex(ValueError,'필수 실행 자산 누락'):runtime_version.validate_boundaries(root)
