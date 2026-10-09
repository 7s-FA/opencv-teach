import unittest,time
from types import SimpleNamespace
from tests.fixtures import load_profile
from so101_teach.domain import JOINTS
from so101_teach.load_compensation import LoadCompensation,MAX_BIAS
class LoadCompensationTests(unittest.TestCase):
    def setUp(self):
        _,self.cal,ref=load_profile();self.goal=ref.middle.copy();self.c=LoadCompensation();self.sent=self.goal.copy()
    def snapshot(self,ticks,load=216,velocity=0):
        return SimpleNamespace(ticks=ticks,fresh=lambda now:True,calibration_matches=True,
            telemetry={n:dict(torque=1,status=0,load_raw=load,temperature_c=40,velocity_signed_raw=velocity,goal_ticks=self.sent[n]) for n in JOINTS})
    def test_both_directions_reduce_41_tick_static_sag_without_mutating_target(self):
        for direction in (-1,1):
            self.c.reset();goal=self.goal.copy();actual={**goal,'shoulder_lift':goal['shoulder_lift']+direction*41};cmd=goal.copy()
            for i in range(240):
                cmd=self.c.command(goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
                actual['shoulder_lift']=cmd['shoulder_lift']+direction*41
            self.assertLessEqual(abs(actual['shoulder_lift']-goal['shoulder_lift']),20)
            self.assertEqual(goal,self.goal);self.assertLessEqual(abs(cmd['shoulder_lift']-goal['shoulder_lift']),MAX_BIAS)
            self.assertEqual(cmd['gripper'],goal['gripper'])
    def test_stuck_joint_stops_instead_of_integrating_forever(self):
        ticks={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        with self.assertRaisesRegex(ValueError,'오차 감소 없음'):
            for i in range(200):self.sent=self.c.command(self.goal,self.snapshot(ticks),self.cal,i*.02,enabled=True)
        self.assertLess(abs(self.c.states['shoulder_lift']['bias']),45)
    def test_moving_or_high_load_or_missing_velocity_does_not_start(self):
        ticks={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        for load,velocity in ((600,0),(216,10),(216,None)):
            self.c.reset()
            for i in range(100):self.assertEqual(self.c.command(self.goal,self.snapshot(ticks,load,velocity),self.cal,i*.02,enabled=True),self.goal)
            self.assertFalse(self.c.states)
    def test_disabled_does_not_change_any_joint(self):
        ticks={n:v+60 for n,v in self.goal.items()}
        for i in range(100):self.assertEqual(self.c.command(self.goal,self.snapshot(ticks),self.cal,i*.02,enabled=False),self.goal)
    def test_high_load_after_start_aborts(self):
        ticks={**self.goal,'elbow_flex':self.goal['elbow_flex']+41}
        for i in range(20):self.c.command(self.goal,self.snapshot(ticks),self.cal,i*.02,enabled=True)
        with self.assertRaises(ValueError):self.c.command(self.goal,self.snapshot(ticks,600),self.cal,.4,enabled=True)
    def test_delayed_poll_never_catches_up_bias(self):
        ticks={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+41}
        for i in range(20):self.c.command(self.goal,self.snapshot(ticks),self.cal,i*.02,enabled=True)
        before=self.c.states['shoulder_lift']['bias'];self.c.command(self.goal,self.snapshot(ticks),self.cal,.8,enabled=True)
        self.assertLessEqual(abs(self.c.states['shoulder_lift']['bias']-before),2.)
    def test_original_goal_arrival_used_not_biased_goal(self):
        ticks={**self.goal,'elbow_flex':self.goal['elbow_flex']+41}
        for i in range(20):self.c.command(self.goal,self.snapshot(ticks),self.cal,i*.02,enabled=True)
        self.assertEqual(self.c.tolerance('elbow_flex',40),20);self.assertEqual(self.c.tolerance('wrist_flex',40),40)
        self.c.reset();self.assertEqual(self.c.tolerance('elbow_flex',40),20)

    def test_small_sag_5_to_39_ticks_corrected_in_both_directions(self):
        for joint in ('shoulder_lift','elbow_flex'):
            for sag in (-39,-20,-5,5,20,39):
                with self.subTest(joint=joint,sag=sag):
                    self.c.reset();actual={**self.goal,joint:self.goal[joint]+sag}
                    for i in range(250):
                        cmd=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
                        actual[joint]=cmd[joint]+sag
                    self.assertLessEqual(abs(actual[joint]-self.goal[joint]),20)
                    self.assertEqual(joint in self.c.states,abs(sag)>20)
    def test_four_ticks_needs_no_additional_push(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+4}
        for i in range(100):self.assertEqual(self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True),self.goal)
        self.assertFalse(self.c.states)
