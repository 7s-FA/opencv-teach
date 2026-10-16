import json,tempfile,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import numpy as np
from so101_teach.domain import ROOT,JOINTS,Calibration,ModelReference
from so101_teach.geometry import Kinematics
from so101_teach.arm_selection_ui import load_preview_mapping,preview_reference,CONFIG_DIR,CONFIG_NAMES,alignment_module
SimulationMapping=alignment_module().SimulationMapping

class SimulationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.profiles=json.loads((ROOT/'data/robot_profiles.json').read_text())
    def pair(self,arm):
        p=next(p for p in self.profiles.values() if p.get('robot_id','arm2')==arm)
        return p,ModelReference(Calibration(ROOT/'data'/p['calibration_file']),p['model_reference'])
    def test_both_arms_use_fixed_mapping_and_preserve_original_reference(self):
        for arm in CONFIG_NAMES:
            p,ref=self.pair(arm);before=deepcopy(ref.__dict__);m,status=load_preview_mapping(p,ref)
            self.assertIsInstance(m,SimulationMapping)
            self.assertIn('시뮬 각도 보정 적용',status);self.assertIn('실행은 기존 식',status)
            for f in ('middle','radians','trims'):self.assertEqual(getattr(ref,f),before[f])
            for fraction in (0.,.25,.5,.75,1.):
                ticks={n:round(v.low+(v.high-v.low)*fraction) for n,v in ref.calibration.motors.items()}
                q=m.angles(ticks);self.assertEqual(m.ticks_from_angles(q),ticks)
                np.testing.assert_allclose(m.align_angles(ref.angles(ticks)),q,atol=1e-12)
            self.assertGreater(np.linalg.norm(np.array(m.angles(ref.middle))-ref.angles(ref.middle)),.01)
    def test_wrong_arm_calibration_angle_mapping_or_model_basis_falls_back(self):
        p,ref=self.pair('arm2');original=json.loads((CONFIG_DIR/CONFIG_NAMES['arm2']).read_text())
        with tempfile.TemporaryDirectory() as d:
            for key in ('robot_id','calibration_sha256','angle_mapping_sha256','source_model_reference'):
                altered=deepcopy(original);altered[key]='wrong'
                (Path(d)/CONFIG_NAMES['arm2']).write_text(json.dumps(altered))
                mapping,status=load_preview_mapping(p,ref,d)
                self.assertIs(mapping,ref);self.assertIn('시뮬 각도 보정 미적용',status)
    def test_missing_and_malformed_mapping_falls_back_visibly(self):
        p,ref=self.pair('arm3')
        with tempfile.TemporaryDirectory() as d:
            for content in (None,'{broken','[]'):
                if content:(Path(d)/CONFIG_NAMES['arm3']).write_text(content)
                m,status=load_preview_mapping(p,ref,d);self.assertIs(m,ref);self.assertIn('시뮬 각도 보정 미적용',status)
    def test_profile_edits_invalidate_display_cache_without_changing_execution_fk(self):
        p,ref=self.pair('arm2');app=SimpleNamespace(profile=p,reference=ref);kin=Kinematics(ref,tcp=p['tcp']);before=kin.fk(ref.middle)
        aligned=preview_reference(app);self.assertIs(aligned,preview_reference(app))
        np.testing.assert_array_equal(kin.fk(ref.middle),before)
        self.assertGreater(np.linalg.norm(kin.fk_angles(aligned.angles(ref.middle))[:3,3]-before[:3,3]),1.)
        ref.radians=ref.radians.copy();ref.radians['wrist_roll']+=.01
        self.assertIs(preview_reference(app),ref);self.assertIn('시뮬 각도 보정 미적용',app.preview_alignment_status)
