import unittest,time
from unittest.mock import Mock,patch
from types import SimpleNamespace
from collections import Counter
from so101_teach.domain import JOINTS,Snapshot
from tests.fixtures import load_profile
from so101_teach.motion import MotionSession,MotionGate,RAM
from so101_teach.devices import ReadOnlyViolation

def packet(mid,op,payload):
    data=[mid,len(payload)+2,op,*payload]
    return bytes([255,255,*data,(~sum(data))&255])

class MotionTests(unittest.TestCase):
    def setUp(self):
        _,self.cal,self.ref=load_profile();self.clock=100.
        self.patcher=patch('so101_teach.motion.time.monotonic',lambda:self.clock);self.patcher.start();self.addCleanup(self.patcher.stop)
        sleeper=patch('so101_teach.communication.time.sleep',lambda seconds:setattr(self,'clock',self.clock+seconds));sleeper.start();self.addCleanup(sleeper.stop)
        self.s=MotionSession('fake',self.cal,bus_factory=lambda *a:None);self.s.running=True
        self.s.stop.wait=lambda duration:setattr(self,'clock',self.clock+duration)
        self.s.heartbeat=1000
        self.values={n:{'Present_Position':v,'Goal_Position':v+50,'Torque_Enable':0,'Status':0,'Present_Temperature':25} for n,v in self.ref.middle.items()}
        self.writes=[];self.auto_on=False;self.drift=0
        def read(reg,n,**kw):
            if reg=='Internal_Goal_Position':return self.values[n].get(reg,self.values[n]['Goal_Position'])
            if self.drift and reg=='Present_Position' and self.values[n]['Torque_Enable']:return self.values[n][reg]+self.drift
            return self.values[n].get(reg,0)
        def write(reg,n,value,**kw):
            self.writes.append((n,reg,value));self.values[n][reg]=value
            if reg=='Goal_Position':
                if self.auto_on:self.values[n]['Torque_Enable']=1
                if self.values[n]['Torque_Enable']:self.values[n]['Present_Position']=value
        self.sync_calls=[]
        def sync_write(reg,values,**kw):
            self.sync_calls.append((reg,values.copy(),kw))
            for n,v in values.items():write(reg,n,v,**kw)
        self.bus=SimpleNamespace(read=read,write=write,sync_write=sync_write,motion_gate=MotionGate(self.cal))
    def snapshot(self,match=True):
        health={n:{'torque':v['Torque_Enable'],'status':v['Status'],'temperature_c':25,'goal_ticks':v['Goal_Position']} for n,v in self.values.items()}
        return Snapshot('follower',{n:v['Present_Position'] for n,v in self.values.items()},health,self.clock,time.time(),self.cal.sha256,match,'fake')
    def test_connect_snapshot_never_writes_or_arms(self):
        self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.writes,[]);self.assertEqual(self.s.state,'READ_ONLY')
    def test_wire_gate_requires_exact_single_use_ram_permit(self):
        gate=MotionGate(self.cal);original=Mock();port=SimpleNamespace(writePort=original);gate.install(port,Counter())
        n=JOINTS[0];mid=self.cal.motors[n].id
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet(mid,3,[40,1]))
        gate.allow(n,'Torque_Enable',0);port.writePort(packet(mid,3,[40,0]))
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet(mid,3,[40,0]))
        gate.allow(n,'Torque_Enable',0)
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet(mid,3,[31,0]))
        with self.assertRaises(ReadOnlyViolation):gate.allow(n,'Homing_Offset',0)
        self.assertEqual(original.call_count,1)
    def test_arm_initializes_current_ticks_and_never_old_goal(self):
        self.s.arm(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD')
        self.assertEqual(self.s.last_goals,self.ref.middle)
        goals=[(n,v) for n,r,v in self.writes if r=='Goal_Position']
        self.assertEqual(dict(goals),self.ref.middle);self.assertEqual(goals[0][0],'wrist_roll')
    def test_bad_internal_target_aborts_even_when_command_and_position_match(self):
        self.values['wrist_roll']['Internal_Goal_Position']=self.ref.middle['wrist_roll']-20
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.s.arm(self.bus,self.snapshot())
        self.s.before_close(self.bus)
        self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
        self.assertFalse(any(n=='shoulder_lift' and r=='Torque_Enable' and v==1 for n,r,v in self.writes))
    def test_internal_target_can_lag_within_segment_but_not_run_past_it(self):
        self.s.check_internal_target('wrist_roll',2010,2000,2050)
        self.s.check_internal_target('wrist_roll',2040,2050,2000)
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.s.check_internal_target('wrist_roll',1989,2000,2050)
    def test_non_position_mode_never_writes_activation(self):
        self.values['wrist_roll']['Operating_Mode']=3
        with self.assertRaisesRegex(RuntimeError,'위치 제어 모드'):self.s.arm(self.bus,self.snapshot())
        self.assertEqual(self.writes,[])
    def test_goal_auto_enable_and_nine_tick_start_drift_no_longer_abort(self):
        self.auto_on=True;self.drift=9
        self.s.arm(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
        self.assertFalse(any(r=='Torque_Enable' and v==0 for n,r,v in self.writes))
    def test_large_unexpected_start_motion_still_aborts_before_shoulders(self):
        self.drift=160
        with self.assertRaises(RuntimeError):self.s.arm(self.bus,self.snapshot())
        self.s.before_close(self.bus)
        self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
        self.assertFalse(any(n=='shoulder_lift' and r=='Torque_Enable' and v==1 for n,r,v in self.writes))
    def test_raw_episode_copies_targets_and_finishes_with_gripper_ticks(self):
        self.s.arm(self.bus,self.snapshot());goal={n:v+12 for n,v in self.ref.middle.items()}
        self.s.request('play',[goal]);goal['gripper']+=100
        self.assertTrue(self.s.program_active.is_set())
        for _ in range(80):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertFalse(self.s.program_active.is_set())
        self.assertEqual(self.s.last_goals['gripper'],self.ref.middle['gripper']+12)
    def test_hold_cancels_queued_move_and_release_has_priority(self):
        self.s.arm(self.bus,self.snapshot());self.s.request('move',[self.ref.middle]);self.s.request('hold')
        self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD');self.assertTrue(self.s.commands.empty());self.assertFalse(self.s.program_active.is_set())
        self.s.request('move',[self.ref.middle]);self.s.request('release');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'READ_ONLY');self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
    def test_short_ui_delay_continues_long_delay_pauses_without_torque_release(self):
        self.s.arm(self.bus,self.snapshot());self.s.request('move',[{n:v+30 for n,v in self.ref.middle.items()}])
        self.s.heartbeat=self.clock-.8;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'MOVING')
        self.s.heartbeat=self.clock-2.1;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertFalse(self.s.program_active.is_set())
        self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
    def test_small_active_error_continues_but_calibration_mismatch_releases(self):
        self.s.arm(self.bus,self.snapshot())
        self.values['wrist_roll']['Present_Position']+=25
        self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD')
        with self.assertRaises(RuntimeError):self.s.on_snapshot(self.bus,self.snapshot(match=False))
        self.s.before_close(self.bus);self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
    def test_delayed_worker_retimes_path_without_jump_or_fault(self):
        self.s.arm(self.bus,self.snapshot());self.s.request('move',[{n:v+100 for n,v in self.ref.middle.items()}])
        self.s.on_snapshot(self.bus,self.snapshot());self.clock+=.4;goals=self.s.last_goals.copy()
        self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'MOVING');self.assertEqual(self.s.last_goals,goals)
    def test_release_failure_attempts_remaining_servos_and_reports_fault(self):
        self.s.arm(self.bus,self.snapshot());orig=self.bus.write
        def fail(reg,n,v,**kwargs):
            if n==JOINTS[0]:raise IOError('wire failure')
            return orig(reg,n,v,**kwargs)
        self.bus.write=fail
        with self.assertRaises(RuntimeError):self.s.release(self.bus)
        self.assertEqual(self.s.state,'FAULT');self.assertTrue(self.s.motion_attempted)
        self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in JOINTS[1:]))
    def test_bounds_rejected_without_sending(self):
        self.s.arm(self.bus,self.snapshot());before=len(self.writes)
        with self.assertRaises(ValueError):self.s.request('move',[{**self.ref.middle,'gripper':4096}])
        self.assertEqual(len(self.writes),before)

    def test_leader_following_maps_gripper_and_stops_holding_on_lost_leader(self):
        from dataclasses import replace
        self.s.arm(self.bus,self.snapshot());lead=replace(self.snapshot(),role='leader');self.s.leader_provider=lambda:lead;self.s.leader_calibration=self.cal
        self.s.request('follow');self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'FOLLOW')
        lead.ticks['shoulder_pan']+=20;self.clock+=.1;self.s.on_snapshot(self.bus,self.snapshot());self.assertGreater(self.s.last_goals['shoulder_pan'],self.ref.middle['shoulder_pan'])
        self.assertEqual(self.s.last_goals['shoulder_pan']-self.ref.middle['shoulder_pan'],20);self.assertEqual(len(self.sync_calls),1)
        self.s.leader_provider=lambda:None;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'FOLLOW');self.clock+=2.1;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD');self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))

    def test_speed_register_units_follow_phase_and_reject_unbounded_speed(self):
        from so101_teach.motion import velocity_register
        for phase,expected in ((0,[8,9,10]),(12,[400,450,500])):
            self.assertEqual([velocity_register(r,phase) for r in (300,350,400)],expected)
        with self.assertRaises(ReadOnlyViolation):self.bus.motion_gate.allow('wrist_roll','Goal_Velocity',0)
        self.s.arm(self.bus,self.snapshot())
        for n in JOINTS:self.values[n]['Phase']=12
        self.writes.clear();self.s.set_speed(300);self.assertFalse(self.writes)
        self.s.request('move',[self.ref.middle]);self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual([v for n,r,v in self.writes if r=='Goal_Velocity'],[400]*6)
        with self.assertRaises(ValueError):self.s.set_speed(300)

    def test_all_speed_presets_complete_both_directions_without_retime_loop(self):
        self.s.arm(self.bus,self.snapshot())
        for rate in (300,350,400):
            for direction in (1,-1):
                self.s.set_speed(rate);start=self.s.last_goals.copy();goal={n:v+direction*48 for n,v in start.items()}
                self.s.request('move',[goal]);self.s.on_snapshot(self.bus,self.snapshot())
                self.assertAlmostEqual(self.s.segment[3],48/(.9*rate))
                previous=start.copy();elapsed=0
                while self.s.state=='MOVING' and elapsed<3:
                    self.clock+=.02;elapsed+=.02;self.s.on_snapshot(self.bus,self.snapshot())
                    for n in JOINTS:
                        delta=self.s.last_goals[n]-previous[n]
                        self.assertGreaterEqual(delta*direction,0);self.assertLessEqual(abs(delta),self.s.max_step_ticks)
                    previous=self.s.last_goals.copy()
                self.assertEqual(self.s.state,'HOLD');self.assertEqual(self.s.last_goals,goal)
                self.assertLess(elapsed,48/rate+.5)

    def test_torque_ack_loss_at_any_start_joint_recovers_without_duplicate_enable(self):
        original=self.bus.write;failed=set()
        def lost_ack(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Torque_Enable' and v==1 and n not in failed:
                failed.add(n);raise ConnectionError('Incorrect status packet!')
        self.bus.write=lost_ack;self.s.arm(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertEqual(len(failed),6)
        for name in JOINTS:self.assertEqual(sum(n==name and r=='Torque_Enable' and v==1 for n,r,v in self.writes),1)
        self.assertEqual(self.s.communication.recoveries,6)

    def test_missed_torque_command_gets_fresh_wire_permit_and_checked_retry(self):
        original=self.bus.write;failed=[]
        def miss(reg,n,v,**kw):
            if n=='wrist_roll' and reg=='Torque_Enable' and v==1 and not failed:
                # The first packet consumed its one-shot permit but was not applied.
                self.bus.motion_gate.permit=None;failed.append(1);raise ConnectionError('No packet')
            self.assertIsNotNone(self.bus.motion_gate.permit)
            original(reg,n,v,**kw)
        self.bus.write=miss;self.s.arm(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD')
        entry=next(r for r in self.s.command_log if r['joint']=='wrist_roll' and r['register']=='Torque_Enable')
        self.assertEqual(entry['attempts'],2);self.assertTrue(entry['acknowledged'])

    def test_applied_goal_with_missing_ack_finishes_episode_without_duplicate_goal(self):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write;failed=set();self.writes.clear()
        def lost(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n not in failed:failed.add(n);raise ConnectionError('ACK lost')
        self.bus.write=lost;goal={n:v+12 for n,v in self.ref.middle.items()};self.s.request('play',[goal])
        for _ in range(80):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertEqual(self.s.last_goals,goal)
        for n in JOINTS:
            values=[v for name,r,v in self.writes if name==n and r=='Goal_Position']
            self.assertEqual(len(values),len(set(values)))

    def test_follow_transfers_measured_angles_between_different_three_point_maps(self):
        from dataclasses import replace
        from so101_teach.angle_mapping import AngleMapping
        from tests.test_angle_calibration import points
        _,leader,_=load_profile();ld=points(leader);fd=points(self.cal)
        ld['joints']['shoulder_pan']['positive']['degrees']=20
        fd['joints']['shoulder_pan']['positive']['degrees']=40
        leader.angle_mapping=AngleMapping(ld,leader.sha256,leader.motors)
        self.cal.angle_mapping=AngleMapping(fd,self.cal.sha256,self.cal.motors)
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'shoulder_pan':2250}
        self.s.leader_calibration=leader;self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=goal.copy())
        self.s.request('follow')
        for _ in range(200):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        expected=round(self.cal.angle_mapping.ticks('shoulder_pan',20))
        self.assertEqual(self.s.last_goals['shoulder_pan'],expected);self.assertNotEqual(expected,2250)
