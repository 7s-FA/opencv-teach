import unittest
from tests import test_motion as fixtures
from so101_teach.motion import BODY_ARRIVAL_TOLERANCE_TICKS

class ArrivalToleranceTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def run_residual(self,residual,targets):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='elbow_flex':self.values[n]['Present_Position']=v+residual
        self.bus.write=write;self.s.request('play',targets)
        for _ in range(500):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.state=='HOLD' and not self.s.command_pending.is_set():break
    def test_three_tick_residual_advances_through_whole_episode(self):
        targets=[{**self.ref.middle,'elbow_flex':self.ref.middle['elbow_flex']+v} for v in (40,80,0)]
        self.run_residual(3,targets)
        self.assertEqual(self.s.completed_request_id,self.s.request_serial);self.assertEqual(self.s.last_goals,targets[-1]);self.assertEqual(self.s.state,'HOLD')
    def test_body_boundary_completes_but_one_tick_beyond_without_feedback_cannot_pass(self):
        goal={**self.ref.middle,'elbow_flex':self.ref.middle['elbow_flex']+40}
        self.run_residual(BODY_ARRIVAL_TOLERANCE_TICKS,[goal]);self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.s.release(self.bus);self.run_residual(BODY_ARRIVAL_TOLERANCE_TICKS+1,[goal]);self.assertNotEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertEqual(self.s.state,'HOLD');self.assertIn('5초 지연',self.s.motion_log[-1]['detail']);self.assertIn(f'팔꿈치 +{BODY_ARRIVAL_TOLERANCE_TICKS+1}틱',self.s.motion_log[-1]['detail']);self.assertTrue(self.s.motion_log[-1]['paused'])
        self.assertEqual(self.s.report['pauses'][-1]['target'],goal)
