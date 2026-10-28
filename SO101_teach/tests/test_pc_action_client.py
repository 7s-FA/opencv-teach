"""PC console and Pi Action contract, without ROS discovery or hardware."""
import ast
import os
import tempfile
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT.parent / 'SO101_PC_Package'
sys.path.insert(0,str(PACKAGE))


class PCActionClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Node:
            def __init__(self, *args, **kwargs): pass
            def get_logger(self): return Mock()
            def create_timer(self,*args): return Mock()
            def destroy_node(self): pass
        modules = {name: ModuleType(name) for name in
                   ['action_msgs.msg', 'rclpy', 'rclpy.action', 'rclpy.node',
                    'rclpy.task', 'host_pkg.action', 'console_ui', 'shared_data']}
        modules['action_msgs.msg'].GoalStatus = SimpleNamespace(STATUS_SUCCEEDED=4,STATUS_ABORTED=6)
        modules['rclpy.node'].Node = Node
        modules['rclpy.action'].ActionClient = lambda *args, **kwargs: Mock()
        modules['rclpy.task'].Future = Future
        modules['host_pkg.action'].Arm = SimpleNamespace(Goal=SimpleNamespace)
        modules['console_ui'].PromptLogger = lambda logger: logger
        modules['console_ui'].console = Mock()
        modules['shared_data'].shared_memory = SimpleNamespace()
        with patch.dict(sys.modules, modules):
            cls.loaded = {}
            for name in ['arm2_node', 'control']:
                spec = importlib.util.spec_from_file_location('audit_pc_' + name, PACKAGE / (name + '.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                cls.loaded[name] = module
        cls.client_module = cls.loaded['arm2_node']
        cls.control_module = cls.loaded['control']

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.dict(os.environ,SO101_CLIENT_STATE_DIR=self.temp.name);p.start();self.addCleanup(p.stop)
        h=patch.object(self.client_module,"ClientHealth",return_value=SimpleNamespace(online=lambda:True,envelope=lambda *args:"wire"));h.start();self.addCleanup(h.stop)

    def client(self):
        client = self.client_module.Arm2ActionClient()
        self.addCleanup(client.destroy_node)
        client._client.server_is_ready.return_value = True
        client._client.send_goal_async.side_effect = lambda *a, **kw: Future()
        return client

    def test_pc_accepts_the_current_pi_command_contract(self):
        tree = ast.parse((ROOT / 'integration/action_server.py').read_text())
        contract = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ('COMMANDS', 'CONTROLS')}
        self.assertEqual(self.client_module.COMMANDS, contract['COMMANDS'])
        self.assertEqual(self.client_module.CONTROLS, set(contract['CONTROLS']))

    def test_restart_result_window_allows_bounded_reinspection(self):
        client=self.client();client.send_goal('RESTART');request=client._requests[-1]
        response=Future();response.set_result(SimpleNamespace(accepted=True,get_result_async=lambda:Future()))
        with patch.object(self.client_module.time,'monotonic',return_value=100):
            client.goal_response_callback(response,request=request)
        self.assertEqual(request['expires'],430.)
    def test_build_done_keeps_job_pending_until_final_result(self):
        client = self.client()
        future = client.send_goal('BUILD_LOAD_A')
        request = client._requests[0]
        client.feedback_callback(SimpleNamespace(feedback=SimpleNamespace(message='BUILD_DONE')), request=request)
        self.assertTrue(client.busy)
        self.assertFalse(future.done())
        self.assertEqual(client.feedback, 'BUILD_DONE')
        result = Future()
        result.set_result(SimpleNamespace(status=4, result=SimpleNamespace(success=True, message='IDLE')))
        client.get_result_callback(result, request=request)
        self.assertFalse(client.busy)
        self.assertTrue(future.result().success)

    def test_clear_error_uses_control_result_without_overwriting_job(self):
        client = self.client()
        job = client.send_goal('BUILD_A')
        control = client.send_goal('CLEAR_ERROR')
        request = client._requests[-1]
        self.assertTrue(request['control'])
        client._finish(request, True, 'IDLE', 'IDLE')
        self.assertTrue(control.result().success)
        self.assertTrue(client.busy)
        self.assertFalse(job.done())
        self.assertIsNone(client.last_result)

    def test_console_routes_home_and_error_clear_to_correct_client(self):
        controller = object.__new__(self.control_module.RobotController)
        controller.arm2 = SimpleNamespace(arm='arm2', busy=False, send_goal=Mock())
        controller.arm3 = SimpleNamespace(arm='arm3', busy=False, send_goal=Mock())
        controller.active_arm = 'arm3'
        controller.console_logger = Mock()
        controller.exit_requested = threading.Event()
        controller.dispatch('PREPARE_HOME')
        controller.arm2.send_goal.assert_called_once_with('PREPARE_HOME')
        controller.arm3.busy = True
        controller.dispatch('CLEAR_ERROR')
        controller.arm3.send_goal.assert_called_once_with('CLEAR_ERROR')

    def test_status_keeps_both_arms_and_uses_korean_without_sending(self):
        controller=object.__new__(self.control_module.RobotController)
        controller.arm2=SimpleNamespace(arm='arm2',communication='ONLINE',busy=False,feedback='IDLE',_client=Mock(),send_goal=Mock())
        controller.arm3=SimpleNamespace(arm='arm3',communication='COMM_LOST',busy=True,feedback='UNKNOWN',_client=Mock(),send_goal=Mock())
        controller.console_logger=Mock();controller.dispatch('STATUS')
        shown=controller.console_logger.info.call_args.args[0]
        self.assertIn('팔2: 연결됨',shown);self.assertIn('팔3: 통신 끊김',shown)
        self.assertIn('결과 미확인',shown);self.assertNotIn('UNKNOWN',shown)
        controller.arm2.send_goal.assert_not_called();controller.arm3.send_goal.assert_not_called()

    def test_pending_retry_and_control_retry_keep_the_real_request_result(self):
        from console_ui import CommandConsole,PromptLogger
        for command,control in [('BUILD_A',False),('CLEAR_ERROR',True)]:
            with self.subTest(command=command):
                client=self.client();display=CommandConsole(log_path=Path(self.temp.name)/'console.log')
                self.addCleanup(display.close);client.console_logger=PromptLogger(None)
                with patch('console_ui.console',display):
                    display.begin_command(command);pending=client.send_goal(command);display.end_command()
                    sent=client._client.send_goal_async.call_count
                    display.begin_command('RETRY_CONTROL' if control else 'RETRY')
                    retry=client.retry_last(control=control);display.end_command()
                    self.assertIs(retry,pending);self.assertFalse(retry.done())
                    self.assertIn('처리 결과 대기',display.commands[-1]['status'])
                    self.assertEqual(client._client.send_goal_async.call_count,sent)
                    request=client._requests[-1]
                    self.assertEqual(display.commands[-1]['request_id'],request['request_id'])
                    client._finish(request,False,'ERROR','실행 실패 확인')
                    self.assertIn('실패',display.commands[-1]['status'])
                    self.assertNotIn('확인 완료',display.commands[-1]['status'])
                # The next fixture uses the same per-arm journal directory.
                client.destroy_node()
