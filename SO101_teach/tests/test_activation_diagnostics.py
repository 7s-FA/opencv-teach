import unittest
from tests import test_motion as fixtures

class ActivationDiagnosticTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def test_stationary_motor_with_nine_tick_internal_difference_is_reported_separately(self):
        n='wrist_roll';actual=self.ref.middle[n];self.auto_on=True
        original=self.bus.write
        def write(register,name,value,**kwargs):
            original(register,name,value,**kwargs)
            if name==n and register=='Goal_Position':self.values[n]['Internal_Goal_Position']=value+9
        self.bus.write=write
        with self.assertRaisesRegex(RuntimeError,'실물 현재') as caught:self.s.arm(self.bus,self.snapshot())
        fault=self.s.report['internal_target_fault']
        self.assertEqual(fault['actual_ticks'],actual);self.assertEqual(fault['command_goal_ticks'],actual)
        self.assertEqual(fault['internal_goal_ticks'],actual+9);self.assertEqual(fault['allowed_margin_ticks'],8)
        self.assertIn('자동 유지 목표',str(caught.exception));self.assertNotIn('전원을',str(caught.exception))
        rows=self.s.report['activation_trace'];self.assertEqual(rows[0]['phase'],'before_goal_write')
        self.assertEqual(rows[0]['torque'],0);self.assertEqual(rows[1]['phase'],'after_goal_write');self.assertEqual(rows[1]['torque'],1)
        self.s.before_close(self.bus);self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
        self.assertFalse(any(name!='wrist_roll' and register=='Goal_Position' for name,register,value in self.writes))
    def test_actual_motion_is_still_reported_as_actual_motion(self):
        self.auto_on=True;self.drift=160
        with self.assertRaisesRegex(RuntimeError,'큰 위치 이탈'):self.s.arm(self.bus,self.snapshot())
        row=self.s.report['activation_trace'][-1]
        self.assertEqual(row['actual_ticks']-row['hold_goal_ticks'],160)
        self.s.before_close(self.bus);self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
    def test_partial_register_failure_retains_which_values_were_observed(self):
        original=self.bus.read
        def read(register,name,**kwargs):
            if register=='Internal_Goal_Position':raise RuntimeError('read failed')
            return original(register,name,**kwargs)
        self.bus.read=read
        with self.assertRaisesRegex(RuntimeError,'read failed'):self.s.activation_sample(self.bus,'wrist_roll',2113,'before_goal_write')
        row=self.s.report['activation_trace'][-1];self.assertIn('actual_ticks',row);self.assertIn('command_goal_ticks',row)
        self.assertNotIn('internal_goal_ticks',row);self.assertNotIn('scan_seconds',row)
