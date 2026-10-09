"""Exercise the deployed controller with fake ROS/GPIO and real temporary files."""
import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from types import ModuleType,SimpleNamespace
from unittest.mock import Mock,patch
from so101_teach.domain import ROOT

class FakeNode:
    def __init__(self,*args,**kwargs):self.logger=Mock();self.publisher=Mock()
    def get_logger(self):return self.logger
    def create_subscription(self,*args):return Mock()
    def create_publisher(self,*args):return self.publisher
    def create_timer(self,*args):return Mock()

class Message:
    def __init__(self,data=0):self.data=data

class LinearCommandMemoryTests(unittest.TestCase):
    def setUp(self):
        gpio=ModuleType('lgpio');gpio.gpiochip_open=Mock(return_value=7);gpio.gpiochip_close=Mock();gpio.tx_servo=Mock(return_value=0)
        ros=ModuleType('rclpy');node=ModuleType('rclpy.node');node.Node=FakeNode
        msgs=ModuleType('std_msgs.msg');msgs.Float32=msgs.Int32=Message
        with patch.dict(sys.modules,{'lgpio':gpio,'rclpy':ros,'rclpy.node':node,'std_msgs':ModuleType('std_msgs'),'std_msgs.msg':msgs}):
            spec=importlib.util.spec_from_file_location('linear_controller_test',ROOT/'tools/linear_controller_pi5.py');self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'state.json';self.m.LAST_COMMAND_FILE=self.path
        self.clock=100.;self.timer=patch.object(self.m.time,'monotonic',side_effect=lambda:self.clock);self.timer.start();self.addCleanup(self.timer.stop)
        self.gpio=gpio
    def test_restore_publishes_history_without_starting_motor(self):
        self.m.store_command(self.path,2000);node=self.m.LinearController();node._publish_state()
        self.gpio.tx_servo.assert_not_called();self.assertEqual(node.publisher.publish.call_args.args[0].data,2000);self.assertIsNone(node._stop_at)
    def test_idle_close_never_sends_redundant_zero_pwm(self):
        self.m.store_command(self.path,1015)
        node=self.m.LinearController();node.close()
        self.gpio.tx_servo.assert_not_called()
        self.gpio.gpiochip_close.assert_called_once_with(7)

    def test_close_after_timed_completion_does_not_stop_pwm_twice(self):
        node=self.m.LinearController();node._command(Message(100.))
        self.clock=108.;node._publish_state();node.close()
        self.assertEqual([c.args[2] for c in self.gpio.tx_servo.call_args_list],[2000,0])

    def test_same_command_never_restarts_pwm_or_extends_deadline(self):
        node=self.m.LinearController();node._command(Message(100.));deadline=node._stop_at
        for now in (101,104,107):self.clock=now;node._command(Message(100.))
        self.gpio.tx_servo.assert_called_once_with(7,18,2000,50);self.assertEqual(node._stop_at,deadline)
        self.clock=108;node._publish_state();self.assertEqual(self.gpio.tx_servo.call_args.args[2],0)
        calls=self.gpio.tx_servo.call_count;node._command(Message(100.));self.assertEqual(self.gpio.tx_servo.call_count,calls);self.assertIsNone(node._stop_at)
    def test_opposite_command_updates_record_and_survives_restart_without_replay(self):
        node=self.m.LinearController();node._command(Message(100.));node._command(Message(1.5))
        self.assertEqual([c.args[2] for c in self.gpio.tx_servo.call_args_list],[2000,1015]);self.assertEqual(self.m.load_last_command(self.path),1015)
        self.gpio.tx_servo.reset_mock();restarted=self.m.LinearController();restarted._command(Message(1.5));restarted._publish_state()
        self.gpio.tx_servo.assert_not_called();self.assertEqual(restarted.publisher.publish.call_args.args[0].data,1015)
        self.assertFalse(json.loads(self.path.read_text())['position_measured'])
    def test_compare_quantized_pulse_not_float_roundoff(self):
        node=self.m.LinearController();node._command(Message(1.5));node._command(Message(1.50000001));self.assertEqual(self.gpio.tx_servo.call_count,1)
    def test_invalid_input_does_not_write_or_move(self):
        node=self.m.LinearController()
        for value in (-1,101,float('nan'),float('inf')):node._command(Message(value))
        self.gpio.tx_servo.assert_not_called();self.assertFalse(self.path.exists());self.assertEqual(node._position,-1)
    def test_unwritable_history_prevents_new_pwm(self):
        node=self.m.LinearController()
        with patch.object(self.m,'store_command',side_effect=PermissionError('read only')):node._command(Message(100.))
        self.gpio.tx_servo.assert_not_called();self.assertEqual(node._position,-1)
    def test_failed_pwm_preserves_previous_accepted_command(self):
        self.m.store_command(self.path,2000);node=self.m.LinearController();self.gpio.tx_servo.return_value=-1
        node._command(Message(1.5));self.assertEqual(node._position,2000);self.assertEqual(self.m.load_last_command(self.path),2000)
        self.gpio.tx_servo.side_effect=RuntimeError('GPIO failed');node._command(Message(1.5));self.assertEqual(self.m.load_last_command(self.path),2000)
    def test_interrupted_pending_record_is_not_restored_as_accepted(self):
        self.m.store_command(self.path,1015,'pending');node=self.m.LinearController()
        self.assertEqual(node._position,-1);self.gpio.tx_servo.assert_not_called()
    def test_commit_failure_does_not_repeat_output_and_duplicate_can_repair_record(self):
        node=self.m.LinearController();store=self.m.store_command
        def fail_accept(path,pulse,status='accepted'):
            if status=='accepted':raise OSError('disk full')
            store(path,pulse,status)
        with patch.object(self.m,'store_command',side_effect=fail_accept):node._command(Message(100.))
        self.assertIsNone(self.m.load_last_command(self.path));self.assertFalse(node._recorded)
        node._command(Message(100.));self.assertEqual(self.gpio.tx_servo.call_count,1);self.assertEqual(self.m.load_last_command(self.path),2000)
    def test_corrupt_record_starts_unknown_without_output(self):
        for raw in ('bad json','[]','{"schema":1,"status":"accepted","pulse_us":true,"position_measured":false}','{"schema":1,"status":"accepted","pulse_us":999,"position_measured":false}'):
            self.path.write_text(raw);node=self.m.LinearController();self.assertEqual(node._position,-1)
        self.gpio.tx_servo.assert_not_called()

class LinearSocketIntegrationTests(unittest.TestCase):
    setUp = LinearCommandMemoryTests.setUp
    def test_preset_socket_rejects_pause_and_preserves_original_completion(self):
        import time as real_time
        from integration.linear_client import LinearClient
        spec=importlib.util.spec_from_file_location('linear_motion',ROOT/'tools/linear_motion.py')
        engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(engine)
        msgs=ModuleType('std_msgs.msg');msgs.String=Message
        with patch.dict(sys.modules,{'linear_motion':engine,'std_msgs':ModuleType('std_msgs'),'std_msgs.msg':msgs}):
            node=self.m.LinearController(control=True)
            client=LinearClient();client.path=self.path.parent/'control.sock'
            try:
                for _ in range(30):
                    if client.path.exists():break
                    real_time.sleep(.01)
                client.call('claim');client.call('ensure',target_mm=100.)
                self.clock=102.
                with self.assertRaisesRegex(RuntimeError,'PAUSE_UNSUPPORTED'): client.call('pause')
                self.assertEqual(client.call('status')['position_state'],'INTERMEDIATE')
                node._publish_state();self.assertEqual(node.publisher.publish.call_args.args[0].data,-1)
                with self.assertRaisesRegex(RuntimeError,'PAUSE_UNSUPPORTED'): client.call('resume')
                self.assertNotIn('remaining_s',client.call('status'))
                self.clock=108.;client.call('status');node._publish_state()
                self.assertEqual(node.publisher.publish.call_args.args[0].data,2000)
                self.assertEqual(client.call('status')['phase'],'TIMED_COMPLETE')
            finally: node.close()
