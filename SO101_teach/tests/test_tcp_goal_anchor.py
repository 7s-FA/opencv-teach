"""Regression for the recorded 2026-10-13 ARM3 Cartesian-target drift."""
import unittest,json
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import numpy as np
from so101_teach.domain import ROOT,read_json,ModelReference
from so101_teach.geometry import Kinematics,corrected_step,corrected_plan,transform_jig_pose,jig_delta
from so101_teach.joint_alignment import ExecutionKinematics,SimulationMapping
from tests import test_execution_alignment as fixtures

EVIDENCE=ROOT/'verification/arm3_execution_failure_20261013'

class TCPGoalAnchorTests(unittest.TestCase):
    inputs=fixtures.ExecutionAlignmentTests.inputs
    def test_taught_transform_keeps_original_position_and_orientation_for_both_arms(self):
        for arm in ('arm2','arm3'):
            p,cal,ref,ep=self.inputs(arm);old=Kinematics(ref,tcp=p['tcp']);new=ExecutionKinematics(p,ref)
            for step in ep['steps']:
                np.testing.assert_array_equal(new.taught_fk(step['ticks']),old.fk(step['ticks']))
            # Changing only the execution map must not move the teaching anchor.
            ticks=ep['steps'][2]['ticks'];anchor=new.taught_fk(ticks)
            new.reference=SimulationMapping(ref,[1.,-2.,3.,-1.,2.,0.],[1]*6,[1]*6)
            np.testing.assert_array_equal(new.taught_fk(ticks),anchor)
            self.assertGreater(np.linalg.norm(new.fk(ticks)-anchor),.01)
    def test_unchanged_jig_still_solves_new_ticks_for_original_tcp_goal(self):
        for arm in ('arm2','arm3'):
            p,cal,ref,ep=self.inputs(arm);old=Kinematics(ref,tcp=p['tcp']);new=ExecutionKinematics(p,ref)
            candidates=[s for s in ep['steps'] if s.get('jig_id')];step=candidates[len(candidates)//2];r=step['jig_reference']
            out=corrected_step(new,step,r,r['stl_sha256'])
            np.testing.assert_array_equal(out['target_xyz_mm'],old.fk(step['ticks'])[:3,3])
            self.assertNotEqual(out['ticks'],step['ticks']);self.assertLessEqual(out['position_error_mm'],3.)
            self.assertEqual(out['ticks']['gripper'],step['ticks']['gripper']);cal.ticks(out['ticks'])
    def test_recorded_build_load_and_load_goals_match_original_golden_coordinates(self):
        _,cal,_,_=self.inputs('arm3');ep=read_json(EVIDENCE/'pi/data/arm3-runtime/episodes/8851f8f6f68343588131edf685bfbf30.json');steps=ep['steps'][1:]
        golden={r['run']:r for r in read_json(EVIDENCE/'tcp_targets.json')['runs']}
        for path in sorted((EVIDENCE/'pi/data/diagnostics/action-failures').glob('*/error.json')):
            ctx=read_json(path)['context'];p=ctx['profile'];ref=ModelReference(cal,p['model_reference']);new=ExecutionKinematics(p,ref)
            before=deepcopy(steps);plan=corrected_plan(new,steps,ctx['fixed_jigs'],lambda k:ctx['mesh_hashes'][k]);self.assertEqual(steps,before)
            for step,result in zip(steps,plan):
                if step.get('jig_id'):
                    expected=next(r for r in golden[ctx['command']]['steps'] if r['step']==step['name'])['original_target_xyz_mm']
                    np.testing.assert_array_equal(result['target_xyz_mm'],expected)
                    self.assertLessEqual(result['position_error_mm'],3.);self.assertLessEqual(result['orientation_error_deg'],45.)
                    self.assertEqual(result['ticks']['gripper'],step['ticks']['gripper'])
                else:self.assertEqual(result['ticks'],step['ticks'])
    def test_initial_and_posture_solvers_use_same_original_full_tcp_transform(self):
        p,cal,ref,ep=self.inputs('arm3');kin=ExecutionKinematics(p,ref);old=Kinematics(ref,tcp=p['tcp']);step=next(s for s in ep['steps'] if s.get('jig_id'))
        r=deepcopy(step['jig_reference']);r['pose'][0]+=24.;r['pose'][2]+=24.
        actual=transform_jig_pose(old.fk(step['ticks']),step['jig_reference']['pose'],jig_delta(step['jig_reference']['pose'],r['pose'],r['symmetry_deg']))
        with patch('so101_teach.geometry.transform_jig_pose',wraps=transform_jig_pose) as transform:
            corrected_step(kin,step,r,r['stl_sha256'])
        self.assertEqual(transform.call_count,2)
        for call in transform.call_args_list:
            np.testing.assert_allclose(transform_jig_pose(*call.args),actual,rtol=0,atol=1e-12)

from tests import test_arm_workspaces as workspace_fixtures

class AppTCPGoalAnchorTests(unittest.TestCase):
    setUp=workspace_fixtures.ArmWorkspacesTests.setUp
    tearDown=workspace_fixtures.ArmWorkspacesTests.tearDown
    def test_app_preview_uses_original_goal_and_original_ik_without_device_commands(self):
        app=self.b;self.manager.select('arm3');app.root.geometry('1920x1080+0+0');app.root.update()
        ctx=read_json(EVIDENCE/'pi/data/diagnostics/action-failures/20261013-192723-c764cf7d210c4773a03337582a3bbf57/error.json')['context']
        ep=read_json(EVIDENCE/'pi/data/arm3-runtime/episodes/8851f8f6f68343588131edf685bfbf30.json');app.episode=deepcopy(ep);steps=ep['steps'][1:]
        with patch.object(app,'motion_request',side_effect=AssertionError('No device commands')),patch.object(app,'connect_devices',side_effect=AssertionError('No device connection')):
            app.begin_execution('preview',steps,ctx['fixed_jigs'])
            index=next(i for i,s in enumerate(steps) if s['name']=='완제품 : 놓기')
            np.testing.assert_allclose(app.last_plan[index]['target_xyz_mm'],[140.0563751334455,177.5283540322367,47.74314532141985],rtol=0,atol=1e-9)
            self.assertIs(type(app.kin),Kinematics)
            self.assertIs(app.kin.reference,app.reference)
            self.assertEqual(app.transport.targets[index],dict(zip(('shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper'),(1356,2142,2026,2965,2162,2400))))
            self.assertEqual(app.transport.targets[index],app.last_plan[index]['ticks'])
            self.assertNotEqual(app.transport.targets[index],steps[index]['ticks']);self.assertIsNone(app.session)
            app.stop_preview()
