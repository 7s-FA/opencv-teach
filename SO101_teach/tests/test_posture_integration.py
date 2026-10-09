import unittest
from copy import deepcopy
from unittest.mock import patch
import numpy as np
from tests import test_remote as remote_fixtures,test_ui as ui_fixtures
from tests.fixtures import DATA,load_profile
from so101_teach.domain import read_json,JOINTS
from so101_teach.geometry import Kinematics,corrected_plan

class RemotePostureTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    rpc=remote_fixtures.RemoteTests.rpc
    def test_pi_and_pc_use_same_sequential_plan_without_motor_commands(self):
        mesh=self.runtime.catalog.mesh('pallet');sha=mesh['sha256']
        ticks=read_json(DATA.parent.parent/'verification/floor-contact/snapshot.json')['ticks']
        steps=[self.runtime.store.step(ticks,'first'),self.runtime.store.step({**ticks,'gripper':ticks['gripper']+10},'second')]
        reference={'pose':[228.,-138.,67.],'symmetry_deg':90,'stl_sha256':sha}
        for s in steps:s.update(jig_id='pallet',jig_reference=deepcopy(reference))
        current={'pallet':{**reference,'pose':[230.,-138.,68.]}}
        expected=corrected_plan(self.runtime.kin,steps,current,lambda key:sha)
        actual=self.rpc('plan',{'steps':steps,'current':current})
        self.assertTrue(actual['ok'],actual);self.assertEqual(actual['value']['plan'],expected);self.assertIsNone(self.runtime.session)

class PreviewPostureTests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    fake_jig_camera=ui_fixtures.UITests.fake_jig_camera
    def test_preview_targets_match_solver_and_report_posture_difference(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True)
        ticks=read_json(DATA.parent.parent/'verification/floor-contact/snapshot.json')['ticks'];a.apply_target(ticks);a.commit_target()
        step=a.episode['steps'][0];ref=step['jig_reference'];current={**ref,'pose':[230.,-138.,68.]}
        expected=corrected_plan(a.kin,[step],current,lambda key:a.catalog.mesh(key)['sha256'])
        with patch.object(a,'motion_request',side_effect=AssertionError('preview must not move motors')):
            a.begin_execution('preview',[step],current)
        self.assertEqual(a.last_plan,expected);self.assertEqual(a.transport.targets,[expected[0]['ticks']]);self.assertIn('수평 기준면 변화',a.message.get());a.stop_preview()

class PathContinuityTests(unittest.TestCase):
    def test_integer_interpolation_stays_in_bounds_and_avoids_new_branch_flips(self):
        profile,cal,ref=load_profile(DATA/'wrist_limit');kin=Kinematics(ref,tcp=profile['tcp']);case=read_json(DATA/'wrist_limit/case.json')
        plan=corrected_plan(kin,case['steps'],case['measured'],lambda key:next(s['jig_reference']['stl_sha256'] for s in case['steps'] if s.get('jig_id')))
        largest_extra=0.
        for a,b,ta,tb in zip(plan,plan[1:],case['steps'],case['steps'][1:]):
            actual=np.degrees(np.array(ref.angles(b['ticks']))-np.array(ref.angles(a['ticks'])))
            taught=np.degrees(np.array(ref.angles(tb['ticks']))-np.array(ref.angles(ta['ticks'])))
            largest_extra=max(largest_extra,float(max(abs(actual[:5]-taught[:5]))))
            for f in np.linspace(0,1,11):cal.ticks({n:round(a['ticks'][n]*(1-f)+b['ticks'][n]*f) for n in JOINTS})
        self.assertLess(largest_extra,30.)
