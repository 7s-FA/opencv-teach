"""No hardware/windows: activation timing, delayed faults and cancellation."""
import threading
import unittest
from tests import test_motion as fixtures
from so101_teach.motion import START_ORDER,ACTIVATION_OBSERVATION_SECONDS


class ActivationOverlapTests(unittest.TestCase):
    snapshot=fixtures.MotionTests.snapshot

    def setUp(self):
        fixtures.MotionTests.setUp(self)
        self.started={};self.io=[];self.io_seconds=.001;self.on_read=None;self.on_write=None
        original_read=self.bus.read;original_write=self.bus.write
        def read(register,name,**kwargs):
            self.clock+=self.io_seconds
            if self.on_read:self.on_read(register,name)
            value=original_read(register,name,**kwargs)
            self.io.append((self.clock,'read',name,register,value,threading.get_ident()))
            return value
        def write(register,name,value,**kwargs):
            self.clock+=self.io_seconds;was_on=self.values[name]['Torque_Enable']
            original_write(register,name,value,**kwargs)
            if not was_on and self.values[name]['Torque_Enable']==1:self.started[name]=self.clock
            self.io.append((self.clock,'write',name,register,value,threading.get_ident()))
            if self.on_write:self.on_write(register,name,value)
        self.bus.read=read;self.bus.write=write

    def arm(self):self.s.arm(self.bus,self.snapshot())

    def assert_off_after_failure(self):
        self.assertNotEqual(self.s.state,'HOLD')
        self.s.before_close(self.bus)
        self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))
        self.assertFalse(self.s.enabled)

    def test_overlap_preserves_wrist_barrier_and_full_observation_for_all(self):
        began=self.clock;self.arm();finished=self.clock
        self.assertEqual(list(self.started),list(START_ORDER))
        self.assertGreaterEqual(self.started['gripper']-self.started['wrist_roll'],.35)
        # Later joints start while their predecessor is still being observed.
        for previous,name in zip(START_ORDER[1:-1],START_ORDER[2:]):
            self.assertLess(self.started[name]-self.started[previous],.35)
        for name,enabled in self.started.items():
            self.assertGreaterEqual(finished-enabled,.35)
            for reg in ('Present_Position','Goal_Position','Internal_Goal_Position','Torque_Enable','Status'):
                last=max(t for t,op,n,r,_,_ in self.io if op=='read' and n==name and r==reg)
                self.assertGreaterEqual(last,enabled+.35)
        self.assertLess(finished-began,1.5)  # Synthetic 1 ms/register bus; old waits alone exceed 2.1 s.
        self.assertEqual(self.s.state,'HOLD');self.assertEqual(self.s.last_goals,self.ref.middle)
        self.assertEqual({row[-1] for row in self.io},{threading.get_ident()})
        self.assertFalse(self.sync_calls)

    def test_implicit_and_explicit_torque_enable_both_get_full_observation(self):
        self.auto_on=True;self.arm()
        self.assertFalse(any(r=='Torque_Enable' and v==1 for _,r,v in self.writes))
        self.assertEqual(len(self.started),6)
        self.assertGreaterEqual(self.clock-max(self.started.values()),ACTIVATION_OBSERVATION_SECONDS)
        self.assertEqual(self.s.state,'HOLD')

    def test_historical_delayed_wrist_fault_still_blocks_all_other_joints(self):
        def fault(reg,name):
            enabled=self.started.get('wrist_roll')
            if enabled is not None and self.clock-enabled>=.17:
                goal=self.ref.middle['wrist_roll']
                self.values['wrist_roll']['Internal_Goal_Position']=goal+9
                self.values['wrist_roll']['Present_Position']=goal+4
        self.auto_on=True;self.on_read=fault
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.arm()
        self.assertEqual(set(self.started),{'wrist_roll'})
        self.assertFalse(any(n!='wrist_roll' and r=='Goal_Position' for n,r,_ in self.writes))
        self.assert_off_after_failure()

    def test_late_fault_in_any_overlapped_joint_never_reaches_hold(self):
        for joint in START_ORDER[1:]:
            with self.subTest(joint=joint):
                # Independent fixture per fault, including its clock/cleanup.
                case=ActivationOverlapTests();case.setUp()
                try:
                    def fault(reg,name):
                        enabled=case.started.get(joint)
                        if enabled is not None and case.clock-enabled>=.17:
                            case.values[joint]['Internal_Goal_Position']=case.ref.middle[joint]+9
                    case.on_read=fault
                    with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):case.arm()
                    self.assertEqual(case.s.report['internal_target_fault']['joint'],joint)
                    case.assert_off_after_failure()
                finally:case.doCleanups()

    def test_previous_joint_fault_during_next_setup_blocks_implicit_enable(self):
        self.auto_on=True
        def fault(reg,name,value):
            if name=='shoulder_pan' and reg=='Goal_Velocity':
                self.values['gripper']['Internal_Goal_Position']=self.ref.middle['gripper']+9
        self.on_write=fault
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.arm()
        self.assertEqual(set(self.started),{'wrist_roll','gripper'})
        self.assertFalse(any(n=='shoulder_pan' and r=='Goal_Position' for n,r,_ in self.writes))
        self.assert_off_after_failure()

    def test_end_of_window_fault_is_checked_before_hold(self):
        self.io_seconds=0.
        def fault(reg,name):
            enabled=self.started.get('wrist_flex')
            if enabled is not None and self.clock>=enabled+.35:
                self.values['wrist_flex']['Internal_Goal_Position']=self.ref.middle['wrist_flex']+9
        self.on_read=fault
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.arm()
        self.assertEqual(set(self.started),set(START_ORDER));self.assert_off_after_failure()

    def test_position_torque_status_and_command_faults_still_stop_activation(self):
        variants=[('Present_Position',lambda goal:goal+129,'큰 위치 이탈'),
                  ('Torque_Enable',lambda goal:0,'시작 상태 이상'),
                  ('Status',lambda goal:4,'시작 상태 이상'),
                  ('Goal_Position',lambda goal:goal+1,'시작 목표 변경')]
        for register,value,error in variants:
            with self.subTest(register=register):
                case=ActivationOverlapTests();case.setUp()
                try:
                    def fault(reg,name):
                        at=case.started.get('gripper')
                        if at is not None and case.clock-at>=.08:
                            case.values['gripper'][register]=value(case.ref.middle['gripper'])
                    case.on_read=fault
                    with self.assertRaisesRegex(RuntimeError,error):case.arm()
                    case.assert_off_after_failure()
                finally:case.doCleanups()

    def test_release_and_disconnect_interrupt_overlapped_observation(self):
        for command in ('release','disconnect'):
            with self.subTest(command=command):
                case=ActivationOverlapTests();case.setUp();cancel_at=[]
                try:
                    def cancel(reg,name):
                        at=case.started.get('shoulder_pan')
                        if not cancel_at and at is not None and case.clock-at>=.03:
                            cancel_at.append(len(case.writes))
                            if command=='release':case.s.request('release')
                            else:case.s.stop.set()
                    case.on_read=cancel
                    with self.assertRaisesRegex(RuntimeError,'토크 해제/연결 종료'):case.arm()
                    self.assertTrue(cancel_at);self.assertEqual(len(case.writes),cancel_at[0])
                    case.assert_off_after_failure()
                    self.assertTrue(all(r=='Torque_Enable' and v==0 for _,r,v in case.writes[cancel_at[0]:]))
                finally:case.doCleanups()

    def test_release_during_final_scan_cannot_transition_to_hold(self):
        def cancel(reg,name):
            at=self.started.get('wrist_flex')
            if at is not None and self.clock>=at+.35 and name=='wrist_flex' and reg=='Status':
                self.s.request('release')
        self.on_read=cancel
        with self.assertRaisesRegex(RuntimeError,'토크 해제/연결 종료'):self.arm()
        self.assert_off_after_failure()

    def test_read_failure_during_overlap_never_becomes_success(self):
        def fail(reg,name):
            if len(self.started)>=3 and name=='gripper' and reg=='Internal_Goal_Position':
                raise ConnectionError('bus read failed')
        self.on_read=fail
        with self.assertRaisesRegex(ConnectionError,'bus read failed'):self.arm()
        self.assert_off_after_failure()

    def test_unknown_implicit_torque_state_never_enables_next_joint(self):
        def fault(reg,name,value):
            if name=='gripper' and reg=='Goal_Position':self.values[name]['Torque_Enable']=2
        self.on_write=fault
        with self.assertRaisesRegex(RuntimeError,'토크 상태 읽기 오류'):self.arm()
        self.assertNotIn('shoulder_pan',self.started);self.assert_off_after_failure()
