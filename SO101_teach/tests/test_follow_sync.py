import unittest
from dataclasses import replace
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_motion as fixtures
from tests.fixtures import load_profile
from so101_teach.domain import JOINTS
from so101_teach.motion import MotionGate,FOLLOW_RATE_TICKS_S
from so101_teach.devices import ReadOnlySession,ReadOnlyViolation

class PassiveLeaderTests(unittest.TestCase):
    def test_brief_voltage_warning_on_complete_feedback(self):
        _,cal,_=load_profile();data=bytearray(31);data[16:18]=(2047).to_bytes(2,'little');data[22]=49
        for role,torque,alarm,comm,size,allowed in [('leader',0,1,0,31,True),('follower',0,1,0,31,True),('leader',1,1,0,31,True),('leader',0,4,0,31,False),('leader',0,1,-1,31,False),('leader',0,1,0,30,False)]:
            with self.subTest(role=role,torque=torque,alarm=alarm,comm=comm,size=size):
                s=ReadOnlySession('fake',cal,role=role);data[0]=torque
                if allowed:
                    tick,h=s.feedback('gripper',data[:size],comm,alarm);self.assertEqual(tick,2047);self.assertEqual(h['packet_alarm'],1);self.assertEqual(s.report['voltage_warnings']['gripper'],1)
                else:
                    with self.assertRaises(RuntimeError):s.feedback('gripper',data[:size],comm,alarm)

class FollowSyncTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def follow(self):
        self.s.arm(self.bus,self.snapshot());self.s.leader_calibration=self.cal;self.goal=self.ref.middle.copy();self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=self.goal.copy())
        self.s.request('follow');self.s.on_snapshot(self.bus,self.snapshot())
        self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot());self.assertFalse(self.s.follow_initial);self.sync_calls.clear()
    def test_sync_gate_checks_every_tick_and_allows_only_one_exact_packet(self):
        gate=self.bus.motion_gate;port=SimpleNamespace(writePort=Mock());gate.install(port,Counter());ticks=self.ref.middle.copy()
        payload=[42,2]
        for n in JOINTS:payload.extend([self.cal.motors[n].id,*ticks[n].to_bytes(2,'little')])
        packet=fixtures.packet(254,0x83,payload)
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet)
        gate.allow_sync_goals(ticks);port.writePort(packet)
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet)
        gate.allow_sync_goals(ticks)
        with self.assertRaises(ReadOnlyViolation):port.writePort(fixtures.packet(254,0x83,[40,1,1,1]))
        with self.assertRaises(ValueError):gate.allow_sync_goals({**ticks,'elbow_flex':4096})
    def test_installed_lerobot_sdk_emits_one_valid_broadcast_without_serial_access(self):
        from so101_teach.motion import motion_bus
        bus=motion_bus('unused',self.cal,Counter());port=bus.port_handler
        port.is_open=True;port.clearPort=Mock();sent=[]
        port.writePort=lambda packet:sent.append(bytes(packet)) or len(packet)
        counts=Counter();bus.motion_gate.install(port,counts);ticks=self.ref.middle.copy()
        bus.motion_gate.allow_sync_goals(ticks);bus.sync_write('Goal_Position',ticks,normalize=False,num_retry=0)
        self.assertEqual(len(sent),1);self.assertEqual(sent[0][2],254);self.assertEqual(sent[0][4:7],bytes([0x83,42,2]));self.assertEqual(counts[0x83],1)
    def test_follow_uses_latest_goal_not_episode_rate_and_one_group_write(self):
        self.follow();self.goal['shoulder_pan']+=300;self.goal['wrist_roll']-=250
        self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.last_goals,self.goal);self.assertEqual(len(self.sync_calls),1)
        self.assertEqual(self.values['shoulder_pan']['Goal_Velocity'],int(FOLLOW_RATE_TICKS_S/50))
        self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
    def test_real_stop_regression_2941_to_2952_does_not_kill_torque(self):
        self.s.arm(self.bus,self.snapshot());n='wrist_flex';self.s.state='FOLLOW';self.s.last_goals[n]=2938
        self.values[n].update(Present_Position=2952,Goal_Position=2938,Internal_Goal_Position=2938)
        snap=self.snapshot();snap.telemetry[n]['internal_goal_ticks']=2938;self.s.last_snapshot=snap
        self.s.pause_motion(self.bus,snap,'리더 수신 중단');self.assertEqual(self.s.state,'HOLD')
        self.values[n].update(Present_Position=2950,Internal_Goal_Position=2941)
        snap=self.snapshot();snap.telemetry[n]['internal_goal_ticks']=2941;self.clock+=.025
        self.s.on_snapshot(self.bus,snap);self.assertEqual(self.s.state,'HOLD');self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
        self.clock+=1;self.values[n].update(Present_Position=2952,Internal_Goal_Position=2952)
        snap=self.snapshot();snap.telemetry[n]['internal_goal_ticks']=2952;self.s.on_snapshot(self.bus,snap)
    def test_internal_target_outside_command_transition_still_rejected(self):
        self.follow();self.goal['wrist_roll']+=80;self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        snap=self.snapshot();snap.telemetry['wrist_roll']['internal_goal_ticks']=1900
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.s.on_snapshot(self.bus,snap)
    def test_dropped_sync_packet_recovers_on_next_latest_frame(self):
        self.follow();self.goal['shoulder_pan']+=200;self.bus.sync_write=Mock();self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'FOLLOW')
        for n,v in self.goal.items():self.values[n].update(Present_Position=v,Goal_Position=v)
        self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.sync_unconfirmed,{});self.assertEqual(self.s.state,'FOLLOW')
