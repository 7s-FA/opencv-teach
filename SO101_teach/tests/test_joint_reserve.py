import unittest
from copy import deepcopy
from dataclasses import replace
import numpy as np
from tests.fixtures import DATA,load_profile
from so101_teach.domain import JOINTS,read_json
from so101_teach.geometry import Kinematics,corrected_step,transform_jig_pose,jig_delta


class JointReserveTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.cal,self.ref=load_profile(DATA/'wrist_limit')
        self.kin=Kinematics(self.ref,tcp=self.profile['tcp'])
        self.case=read_json(DATA/'wrist_limit/case.json');self.step=self.case['steps'][6]
    def limit(self,joint,**limits):self.cal.motors[joint]=replace(self.cal.motors[joint],**limits)
    def solve(self):return corrected_step(self.kin,self.step,self.case['measured'],self.step['jig_reference']['stl_sha256'])
    def validate_pose(self,out):
        r=self.step['jig_reference'];target=transform_jig_pose(self.kin.fk(self.step['ticks']),r['pose'],jig_delta(r['pose'],self.case['measured']['pose'],90))
        self.assertLess(np.linalg.norm(self.kin.fk(out['ticks'])[:3,3]-target[:3,3]),1.)
        self.cal.ticks(out['ticks']);self.assertEqual(out['ticks']['gripper'],self.step['ticks']['gripper'])
    def test_shoulder_upper_limit_is_avoided_with_elbow_and_wrist(self):
        self.limit('shoulder_lift',high=2659);out=self.solve();self.validate_pose(out)
        self.assertGreaterEqual(out['joint_margins_deg']['shoulder_lift'],2.9)
        self.assertGreaterEqual(out['joint_margins_deg']['wrist_flex'],2.9)
    def test_elbow_lower_limit_is_avoided(self):
        self.limit('elbow_flex',low=1299);out=self.solve();self.validate_pose(out)
        self.assertGreaterEqual(out['joint_margins_deg']['elbow_flex'],2.9)
    def test_wrist_rotation_limit_is_avoided_without_changing_gripper(self):
        self.limit('wrist_roll',high=1264);out=self.solve();self.validate_pose(out)
        self.assertGreaterEqual(out['joint_margins_deg']['wrist_roll'],2.9)
        self.assertLessEqual(out['orientation_error_deg'],self.case['old_plan'][6]['orientation_error_deg']+1.)
    def test_unavoidable_base_limit_does_not_prevent_other_joint_clearance(self):
        self.limit('shoulder_pan',low=2284);out=self.solve();self.validate_pose(out)
        self.assertIn('shoulder_pan',out['low_margin_joints']);self.assertFalse(out['joint_margin_satisfied'])
        self.assertGreaterEqual(out['joint_margins_deg']['wrist_flex'],2.9)
    def test_two_joint_limits_are_checked_together(self):
        self.limit('shoulder_lift',high=2659);self.limit('elbow_flex',low=1299)
        out=self.solve();self.validate_pose(out);self.assertTrue(out['joint_margin_satisfied'])
    def test_margins_are_measured_after_integer_tick_conversion(self):
        out=self.solve()
        self.assertEqual(set(out['joint_margins_deg']),set(JOINTS[:5]))
        for name,margin in out['joint_margins_deg'].items():
            angle=self.ref.joint_angle(name,out['ticks'][name]);lo,hi=self.ref.angle_limits(name)
            self.assertAlmostEqual(margin,np.degrees(min(angle-lo,hi-angle)),places=9)
    def test_narrow_range_uses_available_clearance_and_preserves_input(self):
        self.limit('wrist_flex',low=3160);before=deepcopy(self.step);out=self.solve();self.validate_pose(out)
        self.assertLess(out['joint_margins_deg']['wrist_flex'],3.)
        self.assertGreater(out['joint_margins_deg']['wrist_flex'],.4)
        self.assertIn('wrist_flex',out['low_margin_joints']);self.assertEqual(self.step,before)

    def test_multiple_unavoidable_constraints_keep_other_joints_away_from_stops(self):
        self.limit('shoulder_pan',low=2284);self.limit('wrist_roll',low=1258,high=1264)
        out=self.solve();self.validate_pose(out)
        self.assertIn('shoulder_pan',out['low_margin_joints']);self.assertIn('wrist_roll',out['low_margin_joints'])
        self.assertGreaterEqual(out['joint_margins_deg']['wrist_flex'],2.9)
