import unittest
from tests import test_load_compensation as fixtures
class CompensationProgressTests(unittest.TestCase):
    setUp=fixtures.LoadCompensationTests.setUp
    snapshot=fixtures.LoadCompensationTests.snapshot
    def test_recorded_seven_tick_error_is_accepted_without_pushing(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+7}
        for i in range(100):self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
        self.assertEqual(self.sent,self.goal);self.assertFalse(self.c.states)
    def test_fine_stall_waits_for_delivered_twenty_ticks_and_response(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+21}
        stopped=None
        for i in range(250):
            try:self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
            except ValueError as exc:stopped=(i*.02,str(exc));break
        self.assertIsNotNone(stopped);self.assertGreater(stopped[0],1.9);self.assertLess(stopped[0],2.4)
        self.assertIn('오차 감소 없음',stopped[1]);self.assertGreaterEqual(abs(self.sent['shoulder_lift']-self.goal['shoulder_lift']),20)
    def test_one_second_with_eight_tick_bias_does_not_abort(self):
        actual={**self.goal,'elbow_flex':self.goal['elbow_flex']+21}
        for i in range(80):self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
        self.assertLess(abs(self.sent['elbow_flex']-self.goal['elbow_flex']),20)
    def test_only_confirmed_servo_goal_counts_as_delivered(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+21}
        for i in range(170):self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
        self.assertEqual(self.c.states['shoulder_lift']['delivered_since_progress'],0)
    def test_jitter_away_from_goal_is_not_progress(self):
        actual=self.goal.copy()
        with self.assertRaisesRegex(ValueError,'오차 감소 없음'):
            for i in range(250):
                actual['elbow_flex']=self.goal['elbow_flex']+(21 if i<30 or i%2 else 23)
                self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
    def test_actual_error_improvement_restarts_effort_observation(self):
        actual={**self.goal,'shoulder_lift':self.goal['shoulder_lift']+23}
        for i in range(165):
            if i==50:actual['shoulder_lift']=self.goal['shoulder_lift']+20
            self.sent=self.c.command(self.goal,self.snapshot(actual),self.cal,i*.02,enabled=True)
        st=self.c.states['shoulder_lift'];self.assertEqual(st['best_error'],20);self.assertLess(st['delivered_since_progress'],20)
