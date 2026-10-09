import unittest
from so101_teach.gripper_contact import GripperContact
from tests import test_gripper_contact as contact_fixtures
class SustainedContactTests(unittest.TestCase):
    def test_recorded_empty_52_then_44_does_not_hold(self):
        g=GripperContact(50)
        self.assertIsNone(g.observe(2549,2500,{'torque':1,'goal_ticks':2537,'load_raw':52},now=0))
        self.assertEqual(g.constrain(2500),2537)
        for i in range(1,15):
            self.assertIsNone(g.observe(2549,2500,{'torque':1,'goal_ticks':2541,'load_raw':44},now=i*.03))
            self.assertIsNone(g.pending);self.assertIsNone(g.hold_tick)
            self.assertEqual(g.constrain(2500),2500)
    def test_intermittent_high_samples_do_not_accumulate(self):
        g=GripperContact(50)
        for i in range(20):
            h={'torque':1,'goal_ticks':2400,'load_raw':52 if i%3!=2 else 49}
            self.assertIsNone(g.observe(2440,2300,h,now=i*.03))
        self.assertIsNone(g.hold_tick)
    def test_continuous_high_load_holds_at_all_thresholds(self):
        for threshold in (30,50,80,1023):
            g=GripperContact(threshold)
            for i in range(4):self.assertIsNone(g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':threshold},now=i*.03))
            event=g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':threshold},now=.13)
            self.assertEqual(event['hold_tick'],2440)
            self.assertEqual(event['confirmation_rule'],'continuous_threshold')
    def test_confirmation_never_increases_existing_closing_demand(self):
        g=GripperContact(50);g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':52},now=0)
        for target in (2400,2350,2300):self.assertEqual(g.constrain(target),2420)
        self.assertEqual(g.constrain(2450,opening_intent=True),2450);self.assertIsNone(g.pending)
    def test_zero_and_sign_bit_only_clear_pending(self):
        for raw in (0,1024):
            g=GripperContact(50);g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':52},now=0)
            g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':raw},now=.04)
            self.assertIsNone(g.pending);self.assertIsNone(g.hold_tick)
    def test_confirmed_contact_remains_held_until_explicit_open(self):
        g=GripperContact(50)
        for t in (0,.06,.13):g.observe(2440,2300,{'torque':1,'goal_ticks':2420,'load_raw':52},now=t)
        g.observe(2440,2300,{'torque':1,'goal_ticks':2440,'load_raw':0},now=.2)
        self.assertEqual(g.constrain(2300),2440)
        self.assertEqual(g.constrain(2470,opening_intent=True),2470);self.assertIsNone(g.hold_tick)
class TransientMotionTests(unittest.TestCase):
    setUp=contact_fixtures.ContactMotionTests.setUp
    snapshot=contact_fixtures.ContactMotionTests.snapshot
    tick=contact_fixtures.ContactMotionTests.tick
    def test_empty_grip_resumes_target_after_single_spike_at_all_speeds(self):
        self.s.grip_contact=GripperContact(50)
        self.s.arm(self.bus,self.snapshot())
        for speed in (300.,350.,400.):
            self.s.set_speed(speed);start=self.s.last_goals['gripper'];goal={**self.s.last_goals,'gripper':start-80}
            self.s.request('move',[goal]);injected=False;pending_seen=False
            for i in range(200):
                self.values['gripper']['Present_Load']=52 if not injected and i==4 else 44
                if i==4:
                    injected=True
                    self.values['gripper']['Present_Position']=self.values['gripper']['Goal_Position']+2
                self.tick()
                pending_seen=pending_seen or self.s.grip_contact.pending is not None
                if self.s.completed_request_id==self.s.request_serial:break
            self.assertEqual(self.s.completed_request_id,self.s.request_serial)
            self.assertTrue(pending_seen)
            self.assertEqual(self.s.last_goals,goal);self.assertIsNone(self.s.grip_contact.hold_tick)
            self.assertFalse(self.s.report.get('gripper_contacts'))
