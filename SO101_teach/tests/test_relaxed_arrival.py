import unittest
from tests import test_load_comp_motion as fixtures
class RelaxedArrivalTests(unittest.TestCase):
    setUp=fixtures.LoadCompMotionTests.setUp
    snapshot=fixtures.LoadCompMotionTests.snapshot
    def run_offsets(self,offsets):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n in offsets:self.values[n]['Present_Position']=v+offsets[n]
        self.bus.write=write
        targets=[{**self.ref.middle,'shoulder_pan':self.ref.middle['shoulder_pan']+delta,'shoulder_lift':2264+delta,'wrist_flex':2945+delta} for delta in (0,30,60)]
        self.s.request('play',targets)
        for _ in range(1400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id or self.s.report.get('pauses'):break
        return targets
    def test_recorded_shoulder_11_and_wrist_minus13_continue_entire_episode(self):
        targets=self.run_offsets({'shoulder_lift':11,'wrist_flex':-13})
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertFalse(self.s.report.get('pauses'));self.assertEqual(self.s.last_goals,targets[-1])
        self.assertTrue(all(not e['load_compensation'] for e in self.s.report['step_arrivals']))
        self.assertEqual(self.s.report['step_arrivals'][-1]['residual_ticks']['shoulder_lift'],11)
    def test_twenty_tick_boundary_accepts_without_extra_push(self):
        targets=self.run_offsets({'shoulder_lift':20,'elbow_flex':-20,'wrist_flex':20,'wrist_roll':-20})
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        event=self.s.report['step_arrivals'][-1]
        self.assertEqual(event['tolerance_ticks']['shoulder_lift'],20)
        self.assertEqual(event['tolerance_ticks']['wrist_flex'],20)
        self.assertEqual(event['tolerance_ticks']['gripper'],30)
        self.assertFalse(event['load_compensation']);self.assertEqual(event['servo_command'],targets[-1])
    def test_uncompensated_large_error_still_pauses(self):
        self.run_offsets({'wrist_flex':40})
        self.assertIsNone(self.s.completed_request_id);self.assertTrue(self.s.report.get('pauses'))
        self.assertEqual(self.s.state,'HOLD')
