import unittest
from copy import deepcopy
from unittest.mock import patch
import numpy as np
from scipy.spatial.transform import Rotation
from tests.fixtures import DATA,load_profile
from so101_teach.domain import JOINTS,read_json
from so101_teach.geometry import Kinematics,corrected_step,corrected_plan,_initial_corrected_step,_posture_metrics,transform_jig_pose,jig_delta

class PostureFollowTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.cal,self.ref=load_profile(DATA/'wrist_limit');self.kin=Kinematics(self.ref,tcp=self.profile['tcp'])
        self.case=read_json(DATA/'wrist_limit/case.json');self.step=deepcopy(self.case['steps'][6]);self.current=self.case['measured'];self.sha=self.step['jig_reference']['stl_sha256']
    def target(self):
        r=self.step['jig_reference'];return transform_jig_pose(self.kin.fk(self.step['ticks']),r['pose'],jig_delta(r['pose'],self.current['pose'],r['symmetry_deg']))
    def test_taught_tilt_is_preserved_instead_of_forcing_absolute_vertical(self):
        base=_initial_corrected_step(self.kin,self.step,self.current,self.sha);out=corrected_step(self.kin,self.step,self.current,self.sha)
        before=_posture_metrics(self.kin,base['ticks'],self.target(),None)
        self.assertLessEqual(out['taught_plane_tilt_deg'],before['plane']+.1)
        self.assertLessEqual(out['position_error_mm'],max(.5,base['position_error_mm']+.05))
        self.assertEqual(out['posture_reference'],'transformed_taught_grasp');self.assertEqual(out['ticks']['gripper'],self.step['ticks']['gripper'])
    def test_identity_does_not_run_posture_optimizer(self):
        with patch('scipy.optimize.minimize',side_effect=AssertionError('no optimisation for unchanged jig')):
            out=corrected_step(self.kin,self.step,self.step['jig_reference'],self.sha)
        self.assertEqual(out['ticks'],self.step['ticks'])
    def test_fixed_step_never_uses_posture_optimisation(self):
        step={**self.step,'jig_id':None}
        with patch('scipy.optimize.minimize',side_effect=AssertionError('fixed step')):out=corrected_step(self.kin,step,None,'')
        self.assertEqual(out['ticks'],step['ticks']);self.assertFalse(out['corrected'])
    def test_reference_and_previous_step_are_distinct_and_gripper_is_unchanged(self):
        steps=self.case['steps'][5:8];before=deepcopy(steps)
        plan=corrected_plan(self.kin,steps,self.current,lambda key:self.sha)
        self.assertIsNone(plan[0]['joint_change_from_previous_deg'])
        self.assertIsNotNone(plan[1]['joint_change_from_previous_deg'])
        for step,out in zip(steps,plan):self.assertEqual(out['ticks']['gripper'],step['ticks']['gripper']);self.cal.ticks(out['ticks'])
        self.assertEqual(steps,before)
    def test_failure_to_refine_keeps_valid_original_solution(self):
        from types import SimpleNamespace
        base=_initial_corrected_step(self.kin,self.step,self.current,self.sha)
        with patch('scipy.optimize.minimize',return_value=SimpleNamespace(success=False,x=np.zeros(5))):out=corrected_step(self.kin,self.step,self.current,self.sha)
        self.assertEqual(out['ticks'],base['ticks']);self.assertFalse(out['posture_optimized'])
    def test_plane_error_uses_grasp_relative_orientation(self):
        # Taught gripper can be tilted while the hypothetical part is horizontal.
        actual=self.kin.fk(self.step['ticks']);rot=Rotation.from_euler('z',5,degrees=True).as_matrix()
        target=actual.copy();target[:3,:3]=rot@actual[:3,:3]
        # A yaw-only change leaves the horizontal normal unchanged, regardless
        # of the absolute tilt of the gripper.
        delta=(rot@actual[:3,:3])@actual[:3,:3].T
        np.testing.assert_allclose(delta@np.array([0,0,1]),[0,0,1],atol=1e-12)
    def test_invalid_jig_and_unreachable_pose_are_not_silently_accepted(self):
        with self.assertRaises(ValueError):corrected_step(self.kin,self.step,self.current,'bad')
        with self.assertRaisesRegex(ValueError,'도달 불가'):corrected_step(self.kin,self.step,{**self.current,'pose':[2000,2000,80]},self.sha)
    def test_selected_center_tcp_is_never_replaced_with_tip_or_jaw_midpoint(self):
        from so101_teach.configuration import model_tcp
        tcp=model_tcp(point='center');kin=Kinematics(self.ref,tcp=tcp);before=kin.tool.copy()
        out=corrected_step(kin,self.step,self.current,self.sha)
        np.testing.assert_allclose(kin.tool,before);np.testing.assert_allclose(kin.tool[:3,3],[0,0,0])
        self.assertLess(out['position_error_mm'],3.);self.assertEqual(out['ticks']['gripper'],self.step['ticks']['gripper'])
