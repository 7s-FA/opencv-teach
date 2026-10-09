import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace
from tests import test_motion as motion_tests
from tests.fixtures import load_profile
from so101_teach.telemetry import VoltageTracker
from so101_teach.devices import ReadOnlySession
from so101_teach.communication import Communication,install_packet_recovery

class VoltageGraceTests(unittest.TestCase):
    def test_short_alarms_recover_and_do_not_accumulate_across_healthy_feedback(self):
        t=VoltageTracker()
        for start in (0,10,20):
            self.assertEqual(t.observe('gripper',True,start),'warning')
            for i in range(1,10):t.observe('gripper',True,start+i*.4)
            self.assertEqual(t.observe('gripper',False,start+4,complete=True),'recovered')
        self.assertEqual(t.pending,{})
    def test_same_motor_five_seconds_and_other_motor_good_does_not_clear_it(self):
        t=VoltageTracker();t.observe('wrist_roll',True,0)
        for i in range(1,10):t.observe('wrist_roll',True,i*.5);t.observe('gripper',False,i*.5,complete=True)
        with self.assertRaisesRegex(RuntimeError,'전압 경고 5초'):t.observe('wrist_roll',True,5)
    def test_long_unobserved_gap_is_not_counted_as_continuous_alarm(self):
        t=VoltageTracker();t.observe('a',True,0);self.assertEqual(t.observe('a',True,10),'warning')
    def test_feedback_records_raw_alarm_and_reset_without_torque_changes(self):
        _,cal,_=load_profile();s=ReadOnlySession('fake',cal);data=bytearray(31);data[0]=1;data[16:18]=(2047).to_bytes(2,'little');data[22]=80
        with patch('so101_teach.devices.time.monotonic',return_value=0):s.feedback('gripper',data,0,1)
        with patch('so101_teach.devices.time.monotonic',return_value=.2):
            tick,h=s.feedback('gripper',data,0,0)
        self.assertEqual(tick,2047);self.assertEqual(h['torque'],1);self.assertEqual(s.voltage.pending,{})
        self.assertEqual([v['state'] for v in s.report['voltage_events']],['warning','recovered'])
    def test_sdk_read_write_grace_preserves_other_alarms_and_missing_bytes(self):
        read=Mock(side_effect=[([1,2],0,1),([1,2],0,4),([0]*31,0,1)])
        write=Mock(side_effect=[(0,1),(0,4),(-1,1)])
        h=SimpleNamespace(readTxRx=read,writeTxRx=write,ping=Mock())
        io=Communication();io.voltage_handler=Mock();bus=SimpleNamespace(packet_handler=h)
        install_packet_recovery(bus,io)
        self.assertEqual(h.readTxRx(None,1,31,2),([1,2],0,0));self.assertEqual(h.readTxRx(None,1,31,2),([1,2],0,4))
        self.assertEqual(h.readTxRx(None,1,40,31)[2],1)  # Complete feedback keeps raw warning.
        self.assertEqual(h.writeTxRx(None,1,42,2,[0,8]),(0,0));self.assertEqual(h.writeTxRx(None,1,42,2,[0,8]),(0,4));self.assertEqual(h.writeTxRx(None,1,42,2,[0,8]),(-1,1));self.assertEqual(io.voltage_handler.call_count,2)
    def test_cleanup_can_confirm_torque_off_even_after_persistent_voltage_alarm(self):
        _,cal,_=load_profile();s=ReadOnlySession('fake',cal);s.communication.cleaning=True
        s.voltage.observe=Mock(side_effect=RuntimeError('persistent'))
        s.packet_voltage(1);s.voltage.observe.assert_not_called()

class VoltageMotionTests(unittest.TestCase):
    setUp=motion_tests.MotionTests.setUp
    snapshot=motion_tests.MotionTests.snapshot
    def test_short_voltage_status_during_hold_preserves_connection_and_torque(self):
        self.s.arm(self.bus,self.snapshot());self.values['wrist_roll']['Status']=1
        self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD');self.assertTrue(all(v['Torque_Enable']==1 for v in self.values.values()))
        self.values['wrist_roll']['Status']=0;self.clock+=.2;self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD')
    def test_other_status_alarm_still_faults(self):
        self.s.arm(self.bus,self.snapshot());self.values['wrist_roll']['Status']=4
        with self.assertRaisesRegex(RuntimeError,'モータ|모터 상태'):self.s.on_snapshot(self.bus,self.snapshot())

class VoltageNoticeTests(unittest.TestCase):
    def session(self,role):
        _,cal,_=load_profile();return ReadOnlySession('fake',cal,role=role)
    def observe(self,s,now,active):
        with patch('so101_teach.devices.time.monotonic',return_value=now):
            s.observe_voltage('gripper',active,complete=True,value=3.8 if active else 5.1)
    def test_repeated_24ms_dips_recorded_without_warning_or_recovery_notifications(self):
        for role in ('leader','follower'):
            s=self.session(role)
            for t in (0,10,20):self.observe(s,t,True);self.observe(s,t+.024,False)
            self.assertTrue(s.events.empty());self.assertEqual(len(s.report['voltage_events']),6)
            self.assertEqual(s.voltage.pending,{})
    def test_leader_long_voltage_alarm_never_disconnects_and_notifies_once(self):
        s=self.session('leader')
        for i in range(41):self.observe(s,i*.5,True)
        self.assertEqual(s.events.qsize(),1);kind,text=s.events.get_nowait()
        self.assertEqual(kind,'device_notice');self.assertIn('리더',text);self.assertIn('위치 읽기는 계속',text)
        self.observe(s,20.1,False)
        self.assertEqual(s.events.qsize(),1);self.assertIn('해제',s.events.get_nowait()[1])
    def test_notification_waits_for_one_tenth_second(self):
        s=self.session('leader')
        for t in (0,.024,.048,.099):self.observe(s,t,True)
        self.assertTrue(s.events.empty());self.observe(s,.1,True);self.assertEqual(s.events.qsize(),1);self.assertIn('0.1초',s.events.get_nowait()[1])
    def test_follower_still_faults_after_five_seconds(self):
        s=self.session('follower')
        for i in range(10):self.observe(s,i*.5,True)
        with self.assertRaisesRegex(RuntimeError,'전압 경고 5초'):self.observe(s,5,True)
    def test_gap_or_healthy_feedback_cannot_accumulate_short_dips(self):
        s=self.session('leader');self.observe(s,0,True);self.observe(s,3,True)
        self.assertTrue(s.events.empty());self.observe(s,3.1,False);self.observe(s,3.9,True)
        self.assertTrue(s.events.empty())
    def test_leader_still_rejects_other_servo_alarms_and_incomplete_feedback(self):
        s=self.session('leader');data=bytearray(31)
        with self.assertRaisesRegex(RuntimeError,'서보 경고'):s.feedback('gripper',data,0,4)
        with self.assertRaisesRegex(RuntimeError,'통신 오류'):s.feedback('gripper',data[:-1],0,1)
