from collections import Counter
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import math,time,unittest
from unittest.mock import Mock,patch
import numpy as np
from tests.fixtures import load_profile
from tests.test_motion import packet
from so101_teach.domain import JOINTS,Snapshot
from so101_teach.devices import ReadOnlyViolation
from so101_teach.leader_assist import LeaderAssistSession,AssistGate,GravityModel,TARGETS,RESTORED

class LeaderAssistTests(unittest.TestCase):
    def setUp(self):
        _,self.cal,self.ref=load_profile();self.cal.angle_mapping=SimpleNamespace(degrees=lambda n,t:(t-2047)*360/4096)
        self.clock=100.;p=patch('so101_teach.leader_assist.time.monotonic',lambda:self.clock);p.start();self.addCleanup(p.stop)
        self.setup_rig()
    def setup_rig(self):
        self.follow='WAIT';self.s=LeaderAssistSession('fake',self.cal,bus_factory=lambda *a:None);self.s.running=True
        self.values={n:{'Present_Position':(m.low+m.high)//2,'Goal_Position':(m.low+m.high)//2+50,
                       'Torque_Enable':0,'Operating_Mode':0,'I_Coefficient':0,'P_Coefficient':16,
                       'Torque_Limit':1000,'Max_Torque_Limit':1000,'Acceleration':20,'Goal_Time':0,'Goal_Velocity':350,'Phase':4} for n,m in self.cal.motors.items()}
        self.writes=[];self.write_failure=None;self.implicit=False;self.bad_limit=False;self.ignore_restore=None
        def read(reg,n,**kw):
            if reg=='Internal_Goal_Position':return self.values[n]['Goal_Position']
            return self.values[n][reg]
        def write(reg,n,v,**kw):
            if self.write_failure==(n,reg,v):raise RuntimeError('injected write failure')
            self.writes.append((n,reg,v))
            if self.ignore_restore==(n,reg,v):return
            self.values[n][reg]=v
            if reg=='Torque_Limit' and self.bad_limit and v<=100:self.values[n][reg]=1000
            if reg=='Goal_Position' and self.implicit:self.values[n]['Torque_Enable']=1
        self.bus=SimpleNamespace(read=read,write=write,assist_gate=AssistGate(self.cal))
        self.s.latest=self.sample()
    def sample(self):
        h={n:{'torque':v['Torque_Enable'],'status':0,'packet_alarm':0,'temperature_c':25,'goal_ticks':v['Goal_Position'],
              'internal_goal_ticks':v['Goal_Position'],'velocity_signed_raw':0} for n,v in self.values.items()}
        return Snapshot('leader',{n:v['Present_Position'] for n,v in self.values.items()},h,self.clock,time.time(),self.cal.sha256,True,'fake')
    def step(self,dt=.03,sample=None):
        self.clock+=dt;snapshot=sample or self.sample();self.s.latest=snapshot;self.s.on_snapshot(self.bus,snapshot)
    def begin(self,level='약하게'):
        self.s.begin_assist(level,lambda:self.follow,self.ref.radians)
        self.s.model=SimpleNamespace(torques=lambda *a:dict(shoulder_lift=2.,elbow_flex=-2.))
    def activate(self):self.begin();self.follow='RUN';self.step();self.assertEqual(self.s.assist_state,'ACTIVE')
    def test_connection_and_follow_wait_never_energize_leader(self):
        self.step();self.assertFalse(self.writes)
        self.begin();self.step();self.assertFalse(self.writes);self.assertEqual(self.s.assist_state,'WAITING')
        self.follow='STOP';self.step();self.assertEqual(self.s.assist_state,'OFF');self.follow='RUN';self.step();self.assertFalse(self.writes)
    def test_bounded_assistance_and_stop_restore_configuration_not_old_goal(self):
        before=deepcopy(self.values);self.activate()
        for _ in range(80):self.step(.05)
        for n in TARGETS:
            self.assertEqual(self.values[n]['Torque_Limit'],60)
            self.assertLessEqual(abs(self.values[n]['Goal_Position']-before[n]['Present_Position']),12)
        self.assertGreater(self.values['shoulder_lift']['Goal_Position'],before['shoulder_lift']['Present_Position'])
        self.assertLess(self.values['elbow_flex']['Goal_Position'],before['elbow_flex']['Present_Position'])
        self.assertTrue(all(n in TARGETS for n,_,_ in self.writes))
        self.s.stop_assist();self.step()
        for n in TARGETS:
            self.assertEqual(self.values[n]['Torque_Enable'],0)
            for r in RESTORED:self.assertEqual(self.values[n][r],before[n][r])
            self.assertNotEqual(self.values[n]['Goal_Position'],before[n]['Goal_Position'])
        self.assertEqual(self.s.assist_state,'OFF');self.assertIsNone(self.s.assist_error)
    def test_unsupported_mode_integral_or_existing_torque_reject_without_writes(self):
        for reg,value in [('Operating_Mode',2),('I_Coefficient',1),('Torque_Enable',1),('P_Coefficient',0)]:
            with self.subTest(reg=reg):
                self.setup_rig();self.values['shoulder_lift'][reg]=value;self.begin();self.follow='RUN';self.step()
                self.assertFalse(self.writes);self.assertEqual(self.s.assist_state,'FAULT')
    def test_unconfirmed_output_cap_never_reaches_position_or_enable_write(self):
        self.bad_limit=True;self.begin();self.follow='RUN';self.step()
        self.assertEqual(self.s.assist_state,'FAULT')
        self.assertFalse(any(r=='Goal_Position' or r=='Torque_Enable' and v==1 for _,r,v in self.writes))
    def test_implicit_activation_is_capped_and_partial_failure_releases_both(self):
        self.implicit=True;self.begin();self.follow='RUN'
        n='elbow_flex';self.write_failure=(n,'Goal_Position',self.values[n]['Present_Position']);self.step()
        self.assertEqual(self.s.assist_state,'FAULT')
        self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in TARGETS))
        for n in TARGETS:
            limit=next(i for i,row in enumerate(self.writes) if row[0:2]==(n,'Torque_Limit'))
            goals=[i for i,row in enumerate(self.writes) if row[0:2]==(n,'Goal_Position')]
            if goals:self.assertLess(limit,min(goals))
    def test_bad_feedback_or_guard_stops_assistance_and_does_not_auto_resume(self):
        for bad in ('stale','calibration','alarm','temperature','limit','goal','internal','speed','guard'):
            with self.subTest(bad=bad):
                self.setup_rig();self.activate();sample=self.sample();h=deepcopy(sample.telemetry)
                n='shoulder_lift'
                if bad=='stale':sample=replace(sample,monotonic=self.clock-1)
                if bad=='calibration':sample=replace(sample,calibration_matches=False)
                if bad=='alarm':h[n]['status']=1
                if bad=='temperature':h[n]['temperature_c']=55
                if bad=='goal':h[n]['goal_ticks']+=1
                if bad=='internal':h[n]['internal_goal_ticks']+=100
                if bad=='speed':h[n]['velocity_signed_raw']=1401
                if bad=='limit':sample=replace(sample,ticks={**sample.ticks,n:self.cal.motors[n].low})
                if bad=='guard':self.follow='LOST'
                sample=replace(sample,telemetry=h);self.step(sample=sample)
                self.assertEqual(self.s.assist_state,'FAULT');self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in TARGETS))
                size=len(self.writes);self.follow='RUN';self.step();self.assertEqual(len(self.writes),size)
    def test_output_limit_change_is_detected_during_assistance(self):
        self.activate();self.values['shoulder_lift']['Torque_Limit']=1000
        for _ in range(12):self.step(.05)
        self.assertEqual(self.s.assist_state,'FAULT');self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in TARGETS))
    def test_delayed_follower_releases_assistance_but_keeps_passive_reader(self):
        self.activate();self.follow='DELAY';self.step()
        self.assertEqual(self.s.assist_state,'OFF');self.assertIsNone(self.s.assist_error)
        self.assertFalse(self.s.stop.is_set());self.assertTrue(self.s.running)
        self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in TARGETS))
        self.assertTrue(self.s.report['leader_assist']['restored'])
        self.assertTrue(self.s.report['assist_interruptions'])
        writes=len(self.writes);self.follow='RUN';self.step()
        self.assertEqual(len(self.writes),writes)  # No automatic re-energizing.
    def test_delayed_follower_with_failed_release_remains_a_fault(self):
        self.activate();self.follow='DELAY';self.write_failure=('elbow_flex','Torque_Enable',0)
        with self.assertRaisesRegex(RuntimeError,'해제 확인 실패'):self.step()
        self.assertEqual(self.s.assist_state,'FAULT');self.assertIsNotNone(self.s.assist_error)
        self.assertIn('elbow_flex',self.s.touched)
    def test_failed_off_still_releases_other_joint_and_retains_cleanup_retry(self):
        self.activate();self.write_failure=('elbow_flex','Torque_Enable',0);self.s.stop_assist()
        with self.assertRaisesRegex(RuntimeError,'해제 확인 실패'):self.step()
        self.assertEqual(self.values['shoulder_lift']['Torque_Enable'],0);self.assertIn('elbow_flex',self.s.touched)
        self.write_failure=None;self.s.before_close(self.bus);self.assertEqual(self.values['elbow_flex']['Torque_Enable'],0)
    def test_wire_gate_excludes_other_joints_eeprom_and_unpermitted_replay(self):
        gate=AssistGate(self.cal);port=SimpleNamespace(writePort=Mock());gate.install(port,Counter());mid=self.cal.motors['shoulder_lift'].id
        for n,r,v in [('wrist_roll','Torque_Enable',1),('shoulder_lift','Operating_Mode',2),('shoulder_lift','P_Coefficient',10),('elbow_flex','Torque_Limit',101)]:
            with self.assertRaises(ReadOnlyViolation):gate.allow(n,r,v)
        gate.allow('shoulder_lift','Torque_Limit',60);command=packet(mid,3,[48,60,0]);port.writePort(command)
        with self.assertRaises(ReadOnlyViolation):port.writePort(command)
        gate.allow('shoulder_lift','Torque_Enable',0)
        with self.assertRaises(ReadOnlyViolation):port.writePort(packet(mid,3,[31,0,0]))
    def test_nominal_gravity_matches_potential_energy_gradient_without_graphics(self):
        model=GravityModel(self.ref.radians);ticks=self.sample().ticks;forces=model.torques(ticks,self.cal)
        self.assertEqual(model.model.ngeom,0)
        for n in TARGETS:
            index=model.qpos[n];original=model.data.qpos[index];energy=[]
            for delta in (-1e-5,1e-5):
                model.data.qpos[index]=original+delta;model.mujoco.mj_forward(model.model,model.data)
                energy.append(float(np.sum(model.model.body_mass*9.81*model.data.xipos[:,2])))
            model.data.qpos[index]=original
            self.assertAlmostEqual(forces[n],(energy[1]-energy[0])/2e-5,places=5)
        self.cal.angle_mapping=None
        with self.assertRaisesRegex(ValueError,'3점'):model.torques(ticks,self.cal)

    def test_restore_ack_without_matching_register_is_not_reported_as_restored(self):
        self.activate();self.ignore_restore=('elbow_flex','Acceleration',20);self.s.stop_assist()
        with self.assertRaisesRegex(RuntimeError,'원복 읽기 불일치'):self.step()
        self.assertEqual(self.values['elbow_flex']['Torque_Enable'],0)
        self.assertFalse(self.s.report['leader_assist']['restored']);self.assertIn('elbow_flex',self.s.touched)
        self.ignore_restore=None;self.s.before_close(self.bus);self.assertTrue(self.s.report['leader_assist']['restored'])
