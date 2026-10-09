import unittest
from tests import test_load_compensation as fixtures
from tests import test_load_comp_motion as motion_fixtures
from so101_teach.load_compensation import LoadCompensation
from so101_teach.joint_path import JointPath
class FastSettleTests(unittest.TestCase):
    setUp=fixtures.LoadCompensationTests.setUp
    snapshot=fixtures.LoadCompensationTests.snapshot
    def test_tail_observation_never_changes_command_and_avoids_second_wait(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        for i in range(10):
            self.assertEqual(self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=False,prepare_goal=self.goal),self.goal)
        self.assertFalse(self.c.states)
        result=self.c.command(self.goal,self.snapshot(actual),self.cal,.20,enabled=True)
        self.assertLess(result['shoulder_lift'],self.goal['shoulder_lift'])
    def test_tail_with_moving_joint_or_far_command_does_not_prequalify(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        for velocity,delta in ((10,0),(0,3)):
            self.c.reset()
            for i in range(10):self.c.command({**self.goal,'shoulder_lift':self.goal['shoulder_lift']+delta},self.snapshot(actual,velocity=velocity),self.cal,i*.02,enabled=False,prepare_goal=self.goal)
            self.assertEqual(self.c.command(self.goal,self.snapshot(actual),self.cal,.2,enabled=True),self.goal)
            self.assertFalse(self.c.states)
    def test_new_target_or_jitter_invalidates_tail_observation(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        for i in range(10):self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=False,prepare_goal=self.goal)
        moved={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+5}
        self.assertEqual(self.c.command(moved,self.snapshot(actual),self.cal,.2,enabled=True),moved)
        self.assertFalse(self.c.states)
    def test_both_directions_reach_tolerance_without_overshoot_with_response_lag(self):
        for direction in (-1,1):
            self.c.reset();self.sent=self.goal.copy();position=float(self.goal['shoulder_lift']+direction*41)
            for i in range(200):
                actual={**self.goal,'shoulder_lift':round(position)}
                self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
                position+=.2*(self.sent['shoulder_lift']+direction*41-position)
                self.assertGreaterEqual(direction*(position-self.goal['shoulder_lift']),0)
                if abs(round(position)-self.goal['shoulder_lift'])<=20:break
            self.assertLess(i*.02,1.8)
            self.assertLessEqual(abs(round(position)-self.goal['shoulder_lift']),20)
    def test_explicit_grip_stop_survives_contact_clamping(self):
        points=[{**self.goal,'shoulder_lift':self.goal['shoulder_lift']+i*50,'gripper':self.goal['gripper']-(50 if i>=2 else 0)} for i in range(4)]
        original=JointPath(points[0],points[1:],350)
        flags=[x.stop for x in original.segments]
        clamped=[{**p,'gripper':points[0]['gripper']} for p in points[1:]]
        remaining=JointPath(points[0],clamped,350,first_stop=False,stop_flags=flags)
        self.assertTrue(all(seg.stop for seg in remaining.segments))
class FasterMotionTests(unittest.TestCase):
    setUp=motion_fixtures.LoadCompMotionTests.setUp
    snapshot=motion_fixtures.LoadCompMotionTests.snapshot
    move=motion_fixtures.LoadCompMotionTests.move
    def test_compensated_move_has_bounded_commands_into_next_step(self):
        first=self.move();self.s.request_serial
        for _ in range(300):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        prior=self.s.last_goals.copy();second={**first,'shoulder_lift':first['shoulder_lift']-80}
        self.s.request('move',[second])
        for _ in range(300):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            for n in prior:self.assertLessEqual(abs(self.s.last_goals[n]-prior[n]),self.s.max_step_ticks)
            prior=self.s.last_goals.copy()
            if self.s.completed_request_id==self.s.request_serial:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)

    def test_contact_clamp_does_not_remove_original_grip_stop(self):
        from unittest.mock import patch
        self.s.arm(self.bus,self.snapshot());base=self.ref.middle.copy()
        targets=[{**base,'shoulder_lift':base['shoulder_lift']+d,'gripper':base['gripper']-(30 if d>=60 else 0)} for d in (20,40,60,80)]
        with patch.object(self.s.grip_contact,'constrain',side_effect=lambda value,**kw:base['gripper']):
            self.s.request('play',targets)
            for _ in range(400):
                self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
                if self.s.completed_request_id:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertTrue(all(e['arrival_type']=='settled' for e in self.s.report['step_arrivals']))

class ShortRampTests(unittest.TestCase):
    def test_cruise_is_constant_with_short_endpoint_ramps(self):
        from so101_teach.domain import JOINTS
        a=dict.fromkeys(JOINTS,1500);b=dict.fromkeys(JOINTS,2000)
        for speed in (300,350,400):
            seg=JointPath(a,[b],speed).segments[0]
            self.assertAlmostEqual(seg.duration,500/(.9*speed))
            for phase in (.1,.2,.5,.8,.9):
                self.assertAlmostEqual(seg.velocity(phase)['elbow_flex'],speed)
            self.assertAlmostEqual(seg.velocity(0)['elbow_flex'],0)
            self.assertAlmostEqual(seg.velocity(1)['elbow_flex'],0)
    def test_asymmetric_joints_and_reversals_stay_bounded(self):
        import random
        from so101_teach.domain import JOINTS
        rng=random.Random(71)
        for _ in range(20):
            points=[{n:rng.randrange(1200,2800) for n in JOINTS} for _ in range(5)]
            for p in points:p['gripper']=2000
            path=JointPath(points[0],points[1:],350)
            for seg in path.segments:
                for i in range(101):
                    q=seg.sample(i/100);v=seg.velocity(i/100)
                    for n in JOINTS:
                        self.assertLessEqual(abs(v[n]),350.0001)
                        self.assertTrue(min(seg.start[n],seg.goal[n])<=q[n]<=max(seg.start[n],seg.goal[n]))
    def test_profile_is_continuous_at_internal_ramp_boundaries(self):
        from so101_teach.domain import JOINTS
        a=dict.fromkeys(JOINTS,1500);b=dict.fromkeys(JOINTS,2000)
        seg=JointPath(a,[b],350).segments[0]
        for phase in (.1,.9):
            self.assertAlmostEqual(seg.velocity(phase-1e-8)['elbow_flex'],seg.velocity(phase+1e-8)['elbow_flex'],places=6)
            self.assertEqual(seg.sample(phase-1e-8),seg.sample(phase+1e-8))
