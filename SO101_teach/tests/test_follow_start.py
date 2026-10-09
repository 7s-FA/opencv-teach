import unittest
from dataclasses import replace
from tests import test_motion as fixtures
from so101_teach.motion import MotionSession,FOLLOW_START_SPEEDS


class FollowStartTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot

    def start(self,delta=100):
        self.s.arm(self.bus,self.snapshot());self.goal=self.ref.middle.copy();self.goal['shoulder_pan']+=delta
        self.s.leader_calibration=self.cal
        self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=self.goal.copy())
        self.s.request('follow');self.s.on_snapshot(self.bus,self.snapshot())

    def step(self,dt=.02):
        self.clock+=dt;self.s.on_snapshot(self.bus,self.snapshot())

    def test_first_gap_is_limited_then_normal_follow_speed_is_unchanged(self):
        self.start();self.assertEqual(self.s.active_rate,750.);first=self.s.last_goals['shoulder_pan'];self.step()
        self.assertLessEqual(self.s.last_goals['shoulder_pan']-first,8)
        for _ in range(40):self.step()
        self.assertFalse(self.s.follow_initial)
        self.goal['shoulder_pan']+=80;self.step()
        self.assertEqual(self.s.last_goals['shoulder_pan'],self.goal['shoulder_pan'])

    def test_each_setting_changes_only_initial_slew_and_stall_cannot_jump(self):
        self.start()
        for rate in FOLLOW_START_SPEEDS.values():
            self.s.follow_start_rate=rate
            before=self.s.last_goals['shoulder_pan'];self.step(.2)
            self.assertLessEqual(self.s.last_goals['shoulder_pan']-before,round(rate*.05))

    def test_restart_anchors_to_current_position_and_limits_again(self):
        self.start();self.step();self.s.request('hold');self.step()
        before=self.snapshot().ticks.copy();self.goal['shoulder_pan']+=100
        self.s.request('follow');self.step();self.assertEqual(self.s.last_goals,before)
        self.step();self.assertLessEqual(self.s.last_goals['shoulder_pan']-before['shoulder_pan'],8)

    def test_invalid_initial_rate_is_rejected(self):
        for value in (0,-1,1000,float('nan')):
            with self.assertRaises(ValueError):MotionSession('fake',self.cal,follow_start_rate_ticks_s=value)

    def test_regular_750_rate_uses_both_servo_velocity_units(self):
        self.s.arm(self.bus,self.snapshot())
        for phase,expected in ((0,15),(12,750)):
            for values in self.values.values():values['Phase']=phase
            self.s.configure_motion_speed(self.bus,follow=True)
            self.assertTrue(all(v['Goal_Velocity']==expected for v in self.values.values()))
