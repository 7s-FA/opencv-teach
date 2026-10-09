import unittest
from dataclasses import replace
from tests import test_motion as fixtures
class LoadCompMotionTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    def snapshot(self):
        s=fixtures.MotionTests.snapshot(self)
        for h in s.telemetry.values():h.update(load_raw=216,velocity_signed_raw=0,internal_goal_ticks=h['goal_ticks'])
        return s
    def move(self,stuck=False):
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'shoulder_lift':self.ref.middle['shoulder_lift']-60};original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='shoulder_lift':self.values[n]['Present_Position']=(goal[n] if stuck else v)+41
        self.bus.write=write;self.s.request('move',[goal]);return goal
    def test_static_load_reaches_original_goal_with_bias_and_holds(self):
        goal=self.move()
        for _ in range(400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertLessEqual(abs(self.values['shoulder_lift']['Present_Position']-goal['shoulder_lift']),20)
        self.assertNotEqual(self.s.last_goals['shoulder_lift'],goal['shoulder_lift'])
        for _ in range(100):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD')
    def test_stuck_joint_holds_and_records_original_target_and_bias(self):
        goal=self.move(True)
        for _ in range(300):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.report.get('pauses'):break
        self.assertEqual(self.s.state,'HOLD');self.assertIsNone(self.s.completed_request_id)
        self.assertEqual(self.s.report['pauses'][-1]['target'],goal)
        self.assertIn('오차 감소 없음',self.s.report['pauses'][-1]['detail'])
    def test_release_during_bias_clears_state_and_all_torque(self):
        self.move()
        for _ in range(100):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.load_compensation.states:break
        self.assertTrue(self.s.load_compensation.states);self.s.request('release');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertFalse(self.s.load_compensation.states);self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))

    def test_next_reverse_move_keeps_each_original_goal_and_clears_old_bias(self):
        first=self.move();original_goal=first.copy()
        for _ in range(400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id:break
        second={**first,'shoulder_lift':first['shoulder_lift']+80}
        self.s.request('move',[second]);second['shoulder_lift']+=50
        for _ in range(450):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id==self.s.request_serial:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertLessEqual(abs(self.values['shoulder_lift']['Present_Position']-(first['shoulder_lift']+80)),20)
        self.assertEqual(first,original_goal)
    def test_explicit_hold_cancels_correction(self):
        self.move()
        for _ in range(100):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.load_compensation.states:break
        self.s.request('hold');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertFalse(self.s.load_compensation.states)

    def test_success_evidence_separates_original_goal_command_and_actual(self):
        goal=self.move()
        for _ in range(400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id:break
        event=self.s.report['step_arrivals'][-1]
        self.assertEqual(event['target'],goal)
        self.assertNotEqual(event['servo_command']['shoulder_lift'],goal['shoulder_lift'])
        self.assertLessEqual(abs(event['residual_ticks']['shoulder_lift']),20)
        self.assertTrue(event['load_compensation'])
