import unittest
from tests import test_load_comp_motion as fixtures
class EpisodeResidualTests(unittest.TestCase):
    setUp=fixtures.LoadCompMotionTests.setUp
    snapshot=fixtures.LoadCompMotionTests.snapshot
    def start(self,offset=25,joint='shoulder_lift',action='play'):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        goals=[{**self.ref.middle,joint:self.ref.middle[joint]-60},{**self.ref.middle,joint:self.ref.middle[joint]-30}]
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n==joint and self.s.sequence:
                self.values[n]['Present_Position']=self.s.sequence[self.s.index][n]+offset
        self.bus.write=write;self.s.request(action,goals if action=='play' else goals[:1]);return goals
    def run_to_end(self):
        for _ in range(1400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id==self.s.request_serial or self.s.report.get('pauses'):break
    def test_episode_stall_continues_and_records_actual_error(self):
        goals=self.start();self.run_to_end()
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertFalse(self.s.report.get('pauses'))
        rows=self.s.report['step_arrivals'];self.assertEqual(len(rows),2)
        self.assertTrue(all(r['arrival_type']=='residual_accepted' for r in rows))
        self.assertTrue(all(r['residual_ticks']['shoulder_lift']==25 for r in rows))
        self.assertEqual([r['target'] for r in rows],goals)
    def test_manual_move_keeps_its_stop_on_failed_correction(self):
        self.start(action='move');self.run_to_end()
        self.assertIsNone(self.s.completed_request_id);self.assertTrue(self.s.report.get('pauses'))
    def test_32_tick_boundary_continues_but_33_does_not(self):
        self.start(32);self.run_to_end();self.assertEqual(self.s.completed_request_id,self.s.request_serial)
    def test_33_tick_error_still_pauses(self):
        self.start(33);self.run_to_end();self.assertIsNone(self.s.completed_request_id);self.assertTrue(self.s.report.get('pauses'))
    def test_wrist_arrival_timeout_with_small_residual_continues(self):
        self.start(25,'wrist_flex');self.run_to_end()
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertTrue(all(r['arrival_type']=='residual_accepted' for r in self.s.report['step_arrivals']))
    def test_high_load_prevents_soft_acceptance(self):
        self.start();original=self.snapshot
        def snapshot():
            s=original();s.telemetry['shoulder_lift']['load_raw']=600;return s
        self.snapshot=snapshot;self.run_to_end()
        self.assertIsNone(self.s.completed_request_id);self.assertTrue(self.s.report.get('pauses'))
    def test_pending_grip_or_stale_sample_or_moving_arm_cannot_soft_accept(self):
        from dataclasses import replace
        self.s.arm(self.bus,self.snapshot());self.s.episode_run=True;s=self.snapshot();goal=s.ticks.copy()
        self.assertTrue(self.s.can_continue_residual(s,goal,self.clock))
        self.assertFalse(self.s.can_continue_residual(replace(s,monotonic=self.clock-5),goal,self.clock))
        s.telemetry['elbow_flex']['velocity_signed_raw']=8
        self.assertFalse(self.s.can_continue_residual(s,goal,self.clock))
        s.telemetry['elbow_flex']['velocity_signed_raw']=0;self.s.grip_contact.pending={'actual':2047,'initial_goal':2040}
        self.assertFalse(self.s.can_continue_residual(s,goal,self.clock))
    def test_user_stop_during_residual_acceptance_prevents_next_step(self):
        self.start()
        for _ in range(600):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.residual_reason:break
        self.assertTrue(self.s.residual_reason);self.s.request('hold')
        self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertFalse(self.s.sequence)
        self.assertIsNone(self.s.completed_request_id)

    def test_one_moving_sample_during_residual_dwell_does_not_pause(self):
        self.start()
        for _ in range(600):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.residual_reason:break
        self.assertTrue(self.s.residual_reason)
        snapshot=self.snapshot();snapshot.telemetry['shoulder_lift']['velocity_signed_raw']=6
        self.clock+=.02;self.s.on_snapshot(self.bus,snapshot)
        self.assertFalse(self.s.report.get('pauses'));self.assertEqual(self.s.state,'MOVING')
        self.run_to_end();self.assertEqual(self.s.completed_request_id,self.s.request_serial)
