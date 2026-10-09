import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace
from so101_teach.communication import Communication,install_packet_recovery,install_calibration_write_recovery

class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.clock=10.
        for target,replacement in [('time.monotonic',lambda:self.clock),('time.sleep',lambda t:setattr(self,'clock',self.clock+t))]:
            p=patch('so101_teach.communication.'+target,replacement);p.start();self.addCleanup(p.stop)
        self.io=Communication();self.bus=SimpleNamespace(port_handler=SimpleNamespace(ser=SimpleNamespace(reset_input_buffer=Mock())))
    def packets(self,reads,pings=None):
        self.raw_read=Mock(side_effect=reads);self.raw_ping=Mock(side_effect=pings or [(777,0,0)])
        self.bus.packet_handler=SimpleNamespace(readTxRx=self.raw_read,ping=self.raw_ping);install_packet_recovery(self.bus,self.io)
        return self.bus.packet_handler
    def test_feedback_corruption_and_missing_reply_recover_on_third_read(self):
        h=self.packets([([], -1,0),([1],0,0),([0]*31,0,0)])
        self.assertEqual(len(h.readTxRx(None,4,40,31)[0]),31)
        self.assertEqual(self.raw_read.call_count,3);self.assertEqual(self.io.recoveries,1)
        self.assertEqual(self.bus.port_handler.ser.reset_input_buffer.call_count,2)
    def test_servo_alarm_is_returned_once_without_transport_retry(self):
        h=self.packets([([],0,4)])
        self.assertEqual(h.readTxRx(None,4,40,31),([],0,4));self.assertEqual(self.raw_read.call_count,1)
    def test_ping_recovers_without_reopening_port(self):
        h=self.packets([],[(None,-1,0),(777,0,0)])
        self.assertEqual(h.ping(None,5),(777,0,0));self.assertEqual(self.raw_ping.call_count,2)
    def test_persistent_loss_is_bounded_and_never_returns_old_data(self):
        h=self.packets([([], -1,0)]*3)
        with self.assertRaisesRegex(ConnectionError,'3회'):h.readTxRx(None,4,40,31)
        self.assertEqual(self.raw_read.call_count,3)
    def test_slow_attempt_exhausts_time_budget_without_more_requests(self):
        def slow():self.clock+=.3;raise ConnectionError('timeout')
        read=Mock(side_effect=slow)
        with self.assertRaises(ConnectionError):self.io.retry_read(self.bus,'read',read)
        self.assertEqual(read.call_count,1)
    def test_lost_ack_with_value_applied_is_not_written_twice(self):
        send=Mock(side_effect=ConnectionError('lost ACK'));read=Mock(return_value=1)
        self.assertEqual(self.io.verified_write(self.bus,'torque',1,send,read),'readback')
        self.assertEqual(send.call_count,1);self.assertEqual(self.io.events[-1]['outcome'],'verified')
    def test_unapplied_write_is_retried_after_successful_readback_only(self):
        send=Mock(side_effect=[ConnectionError('lost packet'),None]);read=Mock(return_value=0);guard=Mock()
        self.assertEqual(self.io.verified_write(self.bus,'torque',1,send,read,before_retry=guard),'ack')
        self.assertEqual(send.call_count,2);guard.assert_called_once()
    def test_unknown_write_outcome_never_blindly_repeats_torque_on(self):
        send=Mock(side_effect=ConnectionError('lost ACK'));read=Mock(side_effect=ConnectionError('lost read'))
        with self.assertRaises(ConnectionError):self.io.verified_write(self.bus,'torque',1,send,read)
        self.assertEqual(send.call_count,1);self.assertEqual(read.call_count,3)
    def test_servo_alarm_and_gate_errors_are_not_retried(self):
        send=Mock(side_effect=RuntimeError('alarm'));read=Mock()
        with self.assertRaisesRegex(RuntimeError,'alarm'):self.io.verified_write(self.bus,'torque',1,send,read)
        self.assertEqual(send.call_count,1);read.assert_not_called()
    def test_user_stop_cancels_retry_but_cleanup_can_still_turn_torque_off(self):
        def cancel():raise RuntimeError('user stop')
        self.io.cancel=cancel;send=Mock()
        with self.assertRaisesRegex(RuntimeError,'user stop'):self.io.verified_write(self.bus,'torque',1,send,Mock())
        send.assert_not_called()
        with self.io.cleanup():self.io.verified_write(self.bus,'torque',0,send,Mock(),off=True)
        send.assert_called_once()
    def test_calibration_absolute_write_uses_readback_not_whole_recalibration(self):
        self.bus.write=Mock(side_effect=ConnectionError('lost ACK'));original=self.bus.write;self.bus.read=Mock(return_value=-123)
        install_calibration_write_recovery(self.bus,self.io)
        self.bus.write('Homing_Offset','wrist_roll',-123,normalize=False)
        self.assertEqual(original.call_count,1);self.assertEqual(self.io.events[-1]['outcome'],'verified')

    def test_group_read_recovers_and_disables_nested_sdk_retries(self):
        self.bus.sync_read=Mock(side_effect=[ConnectionError('sync loss'),{'wrist_roll':2047}]);original=self.bus.sync_read
        self.packets([])
        self.assertEqual(self.bus.sync_read('Present_Position',normalize=False),{'wrist_roll':2047})
        self.assertEqual(original.call_count,2);self.assertEqual(original.call_args.kwargs['num_retry'],0)

    def test_read_cancellation_is_reported_as_normal_session_close(self):
        from so101_teach.devices import ReadOnlySession
        from tests.fixtures import load_profile
        from so101_teach.communication import CommunicationCancelled
        _,cal,_=load_profile();bus=SimpleNamespace(connect=Mock(),read_calibration=Mock(side_effect=CommunicationCancelled()),port_handler=SimpleNamespace(closePort=Mock()))
        session=ReadOnlySession('fake',cal,bus_factory=lambda *a:bus);session.run()
        self.assertIsNone(session.error);bus.port_handler.closePort.assert_called_once()

class CameraRecoveryTests(unittest.TestCase):
    def test_three_temporary_empty_frames_do_not_disconnect_camera(self):
        import numpy as np
        from so101_teach.devices import CameraSession
        frame=np.zeros((4,4,3),dtype=np.uint8);s=CameraSession(0);clock=[0.]
        s.stop.wait=lambda t:clock.__setitem__(0,clock[0]+t)
        cap=Mock();cap.isOpened.return_value=True;cap.read.side_effect=[(False,None)]*3+[(True,frame)]
        def read():
            if cap.read.call_count<=3:return False,None
            s.stop.set();return True,frame
        cap.read.side_effect=read
        s.processor=lambda f:s.stop.set()
        with patch('cv2.VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',lambda:clock[0]):s.run()
        self.assertIsNone(s.error);self.assertIs(s.frame,frame);self.assertEqual(cap.read.call_count,4);cap.release.assert_called_once()
    def test_camera_persistent_failure_stops_after_grace_without_reusing_frame(self):
        from so101_teach.devices import CameraSession
        s=CameraSession(0);clock=[0.];s.stop.wait=lambda t:clock.__setitem__(0,clock[0]+t)
        cap=Mock();cap.isOpened.return_value=True;cap.read.return_value=(False,None)
        with patch('cv2.VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',lambda:clock[0]):s.run()
        self.assertIn('1초',s.error);self.assertIsNone(s.observation);self.assertLessEqual(cap.read.call_count,22);cap.release.assert_called_once()
