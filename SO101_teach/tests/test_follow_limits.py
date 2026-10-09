import unittest
from dataclasses import replace
from copy import deepcopy
from tests.fixtures import load_profile
from tests.test_angle_calibration import points
from tests import test_motion as fixtures
from so101_teach.domain import JOINTS
from so101_teach.angle_mapping import AngleMapping
from so101_teach.motion import leader_target,FOLLOW_BODY_MARGIN_TICKS

class FollowLimitTests(unittest.TestCase):
    def test_asymmetric_angle_records_not_endpoint_scaling(self):
        _,lc,_=load_profile();_,fc,_=load_profile();ld=points(lc);fd=points(fc)
        for n in JOINTS[:-1]:
            ld['joints'][n]={'negative':{'ticks':1550,'degrees':-90},'zero':{'ticks':2047,'degrees':0},'positive':{'ticks':2500,'degrees':90}}
            fd['joints'][n]={'negative':{'ticks':1300,'degrees':-90},'zero':{'ticks':2047,'degrees':0},'positive':{'ticks':2900,'degrees':90}}
        lc.angle_mapping=AngleMapping(ld,lc.sha256,lc.motors);fc.angle_mapping=AngleMapping(fd,fc.sha256,fc.motors)
        for lt,ft in [(1550,1300),(2047,2047),(2500,2900)]:
            ticks=dict.fromkeys(JOINTS,lt);ticks['gripper']=2047;out=leader_target(ticks,lc,fc)
            self.assertEqual([out[n] for n in JOINTS[:-1]],[ft]*5)
        for tick in range(4096):
            out=leader_target(dict.fromkeys(JOINTS,tick),lc,fc)
            for n,v in out.items():
                m=fc.motors[n];margin=8 if n!='gripper' else 0;self.assertTrue(m.low+margin<=v<=m.high-margin)
    def test_gripper_endpoints_and_body_limits_are_independent(self):
        _,lc,_=load_profile();_,fc,_=load_profile()
        lc.motors['gripper']=replace(lc.motors['gripper'],low=1900,high=3000)
        fc.motors['gripper']=replace(fc.motors['gripper'],low=1500,high=2800)
        for tick,expected in [(0,1500),(1900,1500),(2450,2150),(3000,2800),(4095,2800)]:
            target=leader_target({**dict.fromkeys(JOINTS,2047),'gripper':tick},lc,fc);self.assertEqual(target['gripper'],expected)
    def test_invalid_encoder_value_is_not_hidden_by_clamping(self):
        _,lc,_=load_profile()
        for tick in (-1,4096,float('nan')):
            with self.assertRaises(ValueError):leader_target({**dict.fromkeys(JOINTS,2047),'wrist_roll':tick},lc,lc)

class ContinuousFollowTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def test_one_limited_joint_does_not_stop_other_joints_or_reverse_recovery(self):
        from dataclasses import replace
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'shoulder_lift':0,'wrist_roll':2200}
        self.s.leader_calibration=self.cal;self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=goal.copy())
        self.s.set_speed(300);self.s.request('follow')
        for _ in range(400):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'FOLLOW');self.assertEqual(self.s.last_goals['shoulder_lift'],self.cal.motors['shoulder_lift'].low+8);self.assertEqual(self.s.last_goals['wrist_roll'],2200)
        goal['shoulder_lift']=2100
        for _ in range(400):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'FOLLOW');self.assertEqual(self.s.last_goals['shoulder_lift'],2100)
        for n,reg,v in self.writes:
            if reg=='Goal_Position':self.assertTrue(self.cal.motors[n].low<=v<=self.cal.motors[n].high)
