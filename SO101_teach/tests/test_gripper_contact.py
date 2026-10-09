import unittest
from dataclasses import replace
from tests import test_motion as fixtures
from tests import test_ui as ui_fixtures
from so101_teach.gripper_contact import GripperContact
from so101_teach.domain import read_json

class ContactTests(unittest.TestCase):
    def test_below_threshold_after_pressure_reduction_cancels_confirmation(self):
        g=GripperContact(50)
        self.assertIsNone(g.observe(2277,2266,{'torque':1,'goal_ticks':2266,'load_raw':68},now=0))
        self.assertEqual(g.constrain(2266),2266)
        for t in (.034,.068,.102):
            self.assertIsNone(g.observe(2277,2266,{'torque':1,'goal_ticks':2269,'load_raw':32},now=t))
        event=g.observe(2277,2266,{'torque':1,'goal_ticks':2269,'load_raw':36},now=.136)
        self.assertIsNone(event);self.assertIsNone(g.hold_tick);self.assertEqual(g.constrain(2266),2266)

    def test_low_load_without_our_pressure_reduction_does_not_confirm(self):
        g=GripperContact(50);h={'torque':1,'goal_ticks':2266,'load_raw':68}
        g.observe(2277,2266,h,now=0)
        self.assertIsNone(g.observe(2277,2266,{**h,'load_raw':32},now=.04))
        self.assertIsNone(g.pending)

    def test_pressure_reduction_does_not_confirm_a_moving_or_unloaded_grip(self):
        for actual,load in ((2270,32),(2277,0)):
            g=GripperContact(50)
            g.observe(2277,2266,{'torque':1,'goal_ticks':2266,'load_raw':68},now=0)
            self.assertIsNone(g.observe(actual,2266,{'torque':1,'goal_ticks':2269,'load_raw':load},now=.04))
            self.assertIsNone(g.pending)

    def test_sign_bit_is_not_load_and_only_closing_latches(self):
        g=GripperContact(80)
        self.assertIsNone(g.observe(2200,1800,{'torque':1,'load_raw':1024}))
        self.assertIsNone(g.observe(2200,2400,{'torque':1,'load_raw':1124}))
        self.assertIsNone(g.observe(2200,1800,{'torque':0,'load_raw':1124}))
        for t in (0,.06):self.assertIsNone(g.observe(2200,1800,{'torque':1,'load_raw':1124},now=t))
        self.assertEqual(g.observe(2200,1800,{'torque':1,'load_raw':1124},now=.13)['load_magnitude'],100)
        self.assertEqual(g.constrain(1800),2200)
        self.assertEqual(g.constrain(2203,opening_intent=True),2203);self.assertEqual(g.hold_tick,2200)
        self.assertEqual(g.constrain(2300,opening_intent=True),2300);self.assertIsNone(g.hold_tick)
    def test_one_tick_closing_error_confirms_only_after_stable_load(self):
        g=GripperContact(80)
        for t in (0,.06):self.assertIsNone(g.observe(2200,2199,{'torque':1,'load_raw':80},now=t))
        self.assertIsNotNone(g.observe(2200,2199,{'torque':1,'load_raw':80},now=.13));self.assertEqual(g.constrain(2199),2200)
    def test_fast_start_spike_and_moving_load_do_not_latch(self):
        g=GripperContact(80);h={'torque':1,'load_raw':84,'goal_ticks':2440}
        self.assertIsNone(g.observe(2461,2233,h,now=0))
        self.assertEqual(g.constrain(2233),2440)
        self.assertIsNone(g.observe(2452,2233,{**h,'load_raw':48},now=.04))
        self.assertIsNone(g.pending);self.assertEqual(g.constrain(2233),2233)
        for i in range(10):
            actual=2461-i*8
            self.assertIsNone(g.observe(actual,2233,{**h,'goal_ticks':actual-10},now=1+i*.04))
        self.assertIsNone(g.hold_tick)
    def test_future_goal_opening_and_feedback_gap_do_not_confirm(self):
        g=GripperContact(80);h={'torque':1,'load_raw':84,'goal_ticks':2492}
        self.assertIsNone(g.observe(2488,2233,h,now=0));self.assertIsNone(g.pending)
        h['goal_ticks']=2400
        for t in (.1,.3,.5):self.assertIsNone(g.observe(2461,2233,h,now=t))
        self.assertEqual(g.constrain(2492,opening_intent=True),2492);self.assertIsNone(g.pending)
    def test_invalid_threshold_and_load(self):
        for value in (0,1024,True,80.5):
            with self.assertRaises(ValueError):GripperContact(value)
        with self.assertRaises(ValueError):GripperContact().observe(2200,1800,{'torque':1,'load_raw':65535})

class ContactMotionTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    def snapshot(self,match=True):
        s=fixtures.MotionTests.snapshot(self,match)
        for n,h in s.telemetry.items():h['load_raw']=self.values[n].get('Present_Load',0)
        return s
    def setup_object(self):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='gripper':
                self.values[n]['Present_Position']=max(1950,v)
                self.values[n]['Present_Load']=1124 if v<1950 else 0
        self.bus.write=write
        def sync(reg,values,**kw):
            self.sync_calls.append((reg,values.copy(),kw))
            for n,v in values.items():write(reg,n,v,**kw)
        self.bus.sync_write=sync
    def tick(self):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
    def test_load_50_no_longer_oscillates_until_arrival_timeout(self):
        self.s.grip_contact=GripperContact(50)
        self.values['gripper']['Present_Position']=2300
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='gripper':
                self.values[n]['Present_Position']=max(2277,v)
                self.values[n]['Present_Load']=max(0,(2277-v)*10-44)
        self.bus.write=write
        close={**self.ref.middle,'gripper':2266};lift={**close,'shoulder_pan':2100}
        self.s.request('play',[close,lift])
        for _ in range(900):
            self.tick()
            if self.s.completed_request_id==self.s.request_serial or self.s.report.get('pauses'):break
        self.assertFalse(self.s.report.get('pauses'),self.s.report.get('pauses'))
        self.assertEqual(self.s.completed_request_id,self.s.request_serial)
        self.assertEqual(self.s.grip_contact.hold_tick,2277)
    def test_grasp_lift_and_open_episode_finishes_without_more_closing(self):
        self.setup_object();close={**self.ref.middle,'gripper':1800};lift={**close,'shoulder_pan':2100};opened={**lift,'gripper':2200}
        self.s.request('play',[close,lift,opened]);seen=False;protected=[]
        for _ in range(1600):
            before=len(self.writes);self.tick()
            if self.s.grip_contact.hold_tick is not None:seen=True;protected.extend(self.writes[before:])
            if self.s.completed_request_id==self.s.request_serial:break
        self.assertTrue(seen);self.assertEqual(self.s.completed_request_id,self.s.request_serial);self.assertEqual(self.s.last_goals,opened)
        self.assertTrue(all(v>=1950 for n,reg,v in protected if n=='gripper' and reg=='Goal_Position'))
        self.assertIsNone(self.s.grip_contact.hold_tick);self.assertEqual(self.s.sequence,[])
        self.assertEqual(self.s.report['gripper_contacts'][0]['hold_tick'],1950)
    def test_gripper_contact_does_not_bypass_imprecise_body_arrival(self):
        self.setup_object();original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n in ('shoulder_lift','elbow_flex'):self.values[n]['Present_Position']=v+{'shoulder_lift':36,'elbow_flex':32}[n]
        self.bus.write=write
        close={**self.ref.middle,'gripper':1800};lift={**close,'shoulder_lift':2117,'elbow_flex':2087};opened={**lift,'gripper':2200}
        self.s.request('play',[close,lift,opened])
        for _ in range(1600):
            self.tick()
            if self.s.completed_request_id==self.s.request_serial:break
        self.assertNotEqual(self.s.completed_request_id,self.s.request_serial);self.assertTrue(self.s.report.get('pauses'))
        self.assertTrue(self.s.report['gripper_contacts'])
    def test_follow_contact_survives_repeated_close_and_releases_for_open(self):
        self.setup_object();goal={**self.ref.middle,'gripper':1800}
        self.s.leader_calibration=self.cal;self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=goal.copy());self.s.request('follow')
        for _ in range(200):
            self.tick()
            if self.s.grip_contact.hold_tick is not None:break
        self.assertEqual(self.s.grip_contact.hold_tick,1950);self.assertEqual(self.s.last_goals['gripper'],1950)
        offset=len(self.writes)
        for _ in range(30):self.tick()
        self.assertTrue(all(v>=1950 for n,reg,v in self.writes[offset:] if n=='gripper' and reg=='Goal_Position'))
        goal['gripper']=2200;self.tick();self.assertIsNone(self.s.grip_contact.hold_tick);self.assertEqual(self.s.last_goals['gripper'],2200)
    def test_opening_load_does_not_count_as_closing_contact(self):
        self.s.arm(self.bus,self.snapshot());self.s.last_goals['gripper']=2200;self.values['gripper'].update(Goal_Position=2200,Present_Position=2047,Present_Load=1124)
        self.s.internal_paths['gripper']=(2047,2200,self.clock+2);self.s.protect_gripper(self.bus,self.snapshot())
        self.assertIsNone(self.s.grip_contact.hold_tick)
    def test_all_speeds_ignore_startup_spike_then_wait_for_actual_contact_before_lift(self):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            old=self.values[n]['Present_Position'];original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='gripper':self.values[n]['Present_Position']=old
        self.bus.write=write
        for rate in (300.,350.,400.):
            with self.subTest(rate=rate):
                self.s.set_speed(rate)
                close={**self.ref.middle,'gripper':1800};lift={**close,'shoulder_pan':2100};opened={**lift,'gripper':2200}
                self.s.request('play',[close,lift,opened]);startup=0;had_contact=False
                for _ in range(1800):
                    grip=self.values['gripper'];old=grip['Present_Position'];goal=grip['Goal_Position']
                    desired=max(1950,goal);delta=max(-6,min(6,desired-old));grip['Present_Position']=old+delta
                    closing=goal<old
                    if closing and startup<2:
                        grip['Present_Load']=84;startup+=1
                    else:grip['Present_Load']=100 if closing and grip['Present_Position']==1950 else 40
                    self.tick()
                    if self.s.grip_contact.pending is not None:
                        self.assertGreaterEqual(self.s.last_goals['gripper'],self.s.grip_contact.pending['initial_goal'])
                    if self.s.grip_contact.hold_tick is not None:
                        self.assertEqual(self.s.grip_contact.hold_tick,1950);had_contact=True
                    if self.s.state=='MOVING' and self.s.index>=1:self.assertTrue(had_contact)
                    if self.s.completed_request_id==self.s.request_serial:break
                self.assertTrue(had_contact);self.assertEqual(self.s.completed_request_id,self.s.request_serial)
                self.assertEqual(self.s.last_goals,opened)
    def test_pending_contact_does_not_shorten_endpoint_when_path_is_retimed(self):
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'gripper':1800}
        self.s.request('play',[goal]);self.tick()
        self.s.grip_contact.observe(2000,1800,{'torque':1,'load_raw':84,'goal_ticks':1980},now=self.clock)
        self.s.begin_segment(self.clock)
        self.assertEqual(self.s.segment[1]['gripper'],1800)
    def test_pending_contact_blocks_completion_even_inside_arrival_tolerance(self):
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'gripper':2030}
        self.s.request('play',[goal]);self.tick()
        # Incomplete confirmation must not count as arrival, even after its .2s dwell.
        from unittest.mock import patch
        with patch.object(self.s,'protect_gripper'):
            self.s.grip_contact.pending={'actual':2047,'initial_goal':2039}
            for _ in range(100):self.tick()
        self.assertEqual(self.s.index,0);self.assertIsNone(self.s.completed_request_id)

class ContactSettingsTests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_threshold_persists_without_changing_calibration(self):
        a=self.app;s=a.settings;sha=a.calibration.sha256;s.vars['mode'].set('팔로워 단독');s.vars['grip_contact_load'].set('65');s.save_profile()
        self.assertEqual(read_json(self.data/'profile.json')['grip_contact_load'],65);self.assertEqual(a.calibration.sha256,sha)
