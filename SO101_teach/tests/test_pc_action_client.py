"""PC console and Pi Action contract, without ROS discovery or hardware."""
import ast
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


class PCActionClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Node:
            def __init__(self, *args, **kwargs): pass
            def get_logger(self): return Mock()
        modules = {name: ModuleType(name) for name in
                   ['action_msgs.msg', 'rclpy', 'rclpy.action', 'rclpy.node',
                    'rclpy.task', 'host_pkg.action', 'console_ui', 'shared_data']}
        modules['action_msgs.msg'].GoalStatus = SimpleNamespace(STATUS_SUCCEEDED=4)
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

    def client(self):
        client = self.client_module.Arm2ActionClient()
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
