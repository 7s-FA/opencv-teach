import unittest
from dataclasses import replace
from tests import test_motion as fixtures
from so101_teach.remote_client import RemoteLink

class LeaderDelayTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def follow(self):
        self.s.arm(self.bus,self.snapshot());self.lead=replace(self.snapshot(),role='leader')
        self.s.leader_provider=lambda:self.lead;self.s.leader_calibration=self.cal
        self.s.request('follow');self.s.on_snapshot(self.bus,self.snapshot())
        self.s.on_snapshot(self.bus,self.snapshot());return self.s
    def test_short_gap_holds_actual_pose_then_gradually_recovers(self):
        s=self.follow();n='shoulder_pan';self.clock+=.6
        self.values[n]['Present_Position']-=10;s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(s.state,'FOLLOW');self.assertTrue(s.follow_waiting)
        self.assertEqual(s.last_goals[n],self.values[n]['Present_Position'])
        before=s.last_goals.copy();self.clock+=.02
        self.lead=replace(self.snapshot(),role='leader',ticks={**before,n:before[n]+200})
        s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'FOLLOW');self.assertFalse(s.follow_waiting)
        self.assertGreater(s.last_goals[n],before[n]);self.assertLessEqual(s.last_goals[n]-before[n],20)
        self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
    def test_absent_stream_waits_then_stops_without_restarting_on_late_sample(self):
        s=self.follow();self.lead=None;self.clock+=.1;s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'FOLLOW')
        self.clock+=2.;s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'HOLD')
        before=s.last_goals.copy();self.lead=replace(self.snapshot(),role='leader',ticks={**before,'shoulder_pan':before['shoulder_pan']+100})
        s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.last_goals,before);self.assertEqual(s.state,'HOLD')
    def test_stale_packets_do_not_extend_deadline(self):
        s=self.follow();self.clock+=.6;s.on_snapshot(self.bus,self.snapshot());self.assertTrue(s.follow_waiting)
        self.clock+=1.5;s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'HOLD')
    def test_calibration_failure_stops_immediately(self):
        s=self.follow();self.lead=replace(self.lead,calibration_matches=False)
        s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'HOLD');self.assertFalse(s.follow_waiting)
    def test_manual_stop_during_wait_cannot_resume(self):
        s=self.follow();self.clock+=.6;s.on_snapshot(self.bus,self.snapshot());s.hold_requested.set()
        self.lead=replace(self.snapshot(),role='leader');s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'HOLD')
    def test_gripper_contact_remains_constrained_across_wait_and_recovery(self):
        s=self.follow();held=s.last_goals['gripper'];s.grip_contact.hold_tick=held
        self.clock+=.6;s.on_snapshot(self.bus,self.snapshot());self.clock+=.02
        self.lead=replace(self.snapshot(),role='leader',ticks={**s.last_goals,'gripper':held-100})
        s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.last_goals['gripper'],held)
    def test_stalled_pc_heartbeat_still_stops_immediately(self):
        s=self.follow();s.heartbeat=self.clock-2.1;s.on_snapshot(self.bus,self.snapshot());self.assertEqual(s.state,'HOLD')

class ClockAnchorTests(unittest.TestCase):
    def test_slow_status_reply_does_not_make_fresh_samples_older(self):
        link=RemoteLink({'host':'localhost','user':'robot'})
        link.update_clock_anchor(5100.001,100.,100.003);anchor=link.clock_anchor
        link.update_clock_anchor(5101.001,101.,101.6)
        self.assertEqual(link.clock_anchor,anchor)
        pi_sample=link.clock_anchor[0]+101.59-link.clock_anchor[1]
        self.assertAlmostEqual(5101.6-pi_sample,.012,places=6)
    def test_old_clock_samples_expire(self):
        link=RemoteLink({'host':'localhost','user':'robot'})
        link.update_clock_anchor(5100.001,100.,100.003)
        link.update_clock_anchor(5131.01,131.,131.03)
        self.assertEqual(link.clock_anchor,(5131.01,131.03));self.assertEqual(len(link.clock_samples),1)
