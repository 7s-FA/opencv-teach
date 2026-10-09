import unittest
from copy import deepcopy
from dataclasses import replace
import numpy as np
from tests.fixtures import DATA,load_profile
from so101_teach.domain import JOINTS,read_json
from so101_teach.geometry import Kinematics,corrected_step,transform_jig_pose,jig_delta


class WristReserveTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.cal,self.ref=load_profile(DATA/'wrist_limit')
        self.kin=Kinematics(self.ref,tcp=self.profile['tcp'])
        self.case=read_json(DATA/'wrist_limit/case.json');self.current=self.case['measured']

    def solve(self,step,current=None):
        return corrected_step(self.kin,step,current or self.current,step['jig_reference']['stl_sha256'])

    def target(self,step,current):
        ref=step['jig_reference']
        return transform_jig_pose(self.kin.fk(step['ticks']),ref['pose'],jig_delta(ref['pose'],current['pose'],ref['symmetry_deg']))

    def test_recorded_placement_keeps_fixed_tcp_target_and_avoids_wrist_stop(self):
        before=deepcopy(self.case)
        for step,old in zip(self.case['steps'],self.case['old_plan']):
            if not step.get('jig_id'):continue
            out=self.solve(step);actual=self.kin.fk(out['ticks']);target=self.target(step,self.current)
            self.assertLess(np.linalg.norm(actual[:3,3]-target[:3,3]),.5)
            self.assertGreaterEqual(out['wrist_margin_deg'],2.9)
            self.assertLess(out['ticks']['wrist_flex'],self.cal.motors['wrist_flex'].high-30)
            self.assertEqual(out['ticks']['gripper'],step['ticks']['gripper'])
            self.assertNotEqual(out['ticks']['shoulder_lift'],old['ticks']['shoulder_lift'])
            self.assertNotEqual(out['ticks']['elbow_flex'],old['ticks']['elbow_flex'])
            self.cal.ticks(out['ticks'])
        self.assertEqual(self.case,before)
        self.assertEqual(self.profile['tcp']['mode'],'model')

    def test_identity_and_fixed_steps_preserve_taught_ticks(self):
        for step in self.case['steps']:
            current=step.get('jig_reference');sha=current['stl_sha256'] if current else ''
            out=corrected_step(self.kin,step,current,sha)
            self.assertEqual(out['ticks'],step['ticks'])

    def test_tight_mechanical_range_reports_missing_reserve_without_exceeding_limits(self):
        step=deepcopy(self.case['steps'][5])
        self.cal.motors['wrist_flex']=replace(self.cal.motors['wrist_flex'],low=3200)
        out=self.solve(step)
        self.assertFalse(out['wrist_margin_satisfied']);self.assertLess(out['wrist_margin_deg'],3)
        self.assertLess(out['position_error_mm'],3);self.cal.ticks(out['ticks'])

    def test_manual_tcp_and_gripper_are_not_replaced_by_jaw_centre(self):
        tcp={'mode':'manual','xyz_mm':[4,2,9],'rpy_deg':[0,0,0]};before=deepcopy(tcp)
        self.kin=Kinematics(self.ref,tcp=tcp);step=self.case['steps'][6];out=self.solve(step)
        np.testing.assert_allclose(self.kin.fk(out['ticks'])[:3,3],self.target(step,self.current)[:3,3],atol=.6)
        self.assertEqual(tcp,before);self.assertEqual(out['ticks']['gripper'],step['ticks']['gripper'])

    def test_unreachable_position_still_reports_failure_without_changing_taught_data(self):
        step=self.case['steps'][6];before=deepcopy(step)
        with self.assertRaisesRegex(ValueError,'도달 불가'):self.solve(step,{**self.current,'pose':[2000,2000,80]})
        self.assertEqual(step,before)

    def test_recorded_path_interpolation_stays_inside_all_joint_ranges(self):
        plan=[self.solve(s) if s.get('jig_id') else {'ticks':s['ticks']} for s in self.case['steps']]
        for a,b in zip(plan,plan[1:]):
            for f in np.linspace(0,1,11):
                self.cal.ticks({n:round(a['ticks'][n]*(1-f)+b['ticks'][n]*f) for n in JOINTS})
