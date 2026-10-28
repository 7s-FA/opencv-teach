import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

DIR = Path(__file__).parents[1] / 'integration'
sys.path.insert(0, str(DIR))
import action_protocol as protocol


class FakeNode:
    def __init__(self, *args, **kwargs): self.logger = Mock()
    def get_logger(self): return self.logger
    def create_publisher(self,*args,**kwargs):return Mock()


class FakeArm:
    Result = SimpleNamespace
    Feedback = staticmethod(lambda: SimpleNamespace(message=''))


class Goal:
    def __init__(self, command):
        self.meta=dict(version=2, command=command, client_id='c'*32, request_id=uuid.uuid4().hex, attempt_id=uuid.uuid4().hex, epoch='test', nonce='test'); self.request = SimpleNamespace(command=json.dumps(self.meta)); self.messages = []; self.is_active = True
        self.succeeded = self.aborted = False
    def publish_feedback(self, feedback): self.messages.append(feedback.message)
    def succeed(self): self.succeeded = True; self.is_active = False
    def abort(self): self.aborted = True; self.is_active = False


class Process:
    def __init__(self, lines, code=0): self.stdout = [line+'\n' for line in lines]; self.code = code;self.stdin=io.StringIO()
    def wait(self, **kw): return self.code
    def poll(self): return self.code


class ActionContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ros = ModuleType('rclpy'); action = ModuleType('rclpy.action'); callbacks = ModuleType('rclpy.callback_groups')
        executor = ModuleType('rclpy.executors'); node = ModuleType('rclpy.node'); host = ModuleType('host_pkg.action')
        action.ActionServer = Mock(); action.GoalResponse = SimpleNamespace(ACCEPT=True, REJECT=False)
        action.CancelResponse = SimpleNamespace(REJECT=False); callbacks.ReentrantCallbackGroup = Mock()
        executor.ExternalShutdownException = type('ExternalShutdownException', (Exception,), {}); executor.MultiThreadedExecutor = Mock(); node.Node = FakeNode; host.Arm = FakeArm
        duration=ModuleType('rclpy.duration');duration.Duration=SimpleNamespace
        messages=ModuleType('std_msgs.msg');messages.String=SimpleNamespace
        with patch.dict(sys.modules, {'rclpy':ros,'rclpy.action':action,'rclpy.callback_groups':callbacks,'rclpy.executors':executor,'rclpy.node':node,'rclpy.duration':duration,'std_msgs':ModuleType('std_msgs'),'std_msgs.msg':messages,'host_pkg':ModuleType('host_pkg'),'host_pkg.action':host}):
            spec=importlib.util.spec_from_file_location('action_contract_server',DIR/'action_server.py')
            cls.module=importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.module)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(self.module,'ROOT',Path(self.temp.name));p.start();self.addCleanup(p.stop)
        h=patch.object(self.module,'SupervisionHealth',return_value=SimpleNamespace(epoch='test',accepts=lambda meta:True,fresh=lambda client:True,control_fence=lambda meta,state:True));h.start();self.addCleanup(h.stop)
        self.server = self.module.ArmActionServer('arm2', threading.Lock(), protocol)
        self.server.gate.status_ready_at=0. # Fake DDS is already discovered.
        self.addCleanup(lambda:self.server.gate.retire(self.server.gate.active))

    def execute(self, arm, goal):
        self.server.arm = arm
        self.assertTrue(self.server.goal_callback(goal.request))
        self.server.logger.info.reset_mock()
        return self.server.execute_callback(goal)
    def test_completion_handoff_accepts_next_goal_and_old_cleanup_keeps_its_lock(self):
        goal=Goal('BUILD_A');process=Process(['READY_TO_COMPLETE','A_DONE']);accepted=[]
        def ack(value):
            self.assertEqual(value,'COMPLETE\n');self.assertFalse(self.server.job_lock.locked())
            accepted.append(self.server.goal_callback(Goal('BUILD_B').request))
        process.stdin=SimpleNamespace(write=ack,flush=lambda:None)
        with patch.object(self.module.subprocess,'Popen',return_value=process):result=self.execute('arm2',goal)
        self.assertTrue(result.success);self.assertEqual(accepted,[True]);self.assertTrue(self.server.job_lock.locked())
        self.server.job_lock.release();self.server.dispatch_lock.close();self.server.dispatch_lock=None

    def test_details_stay_in_logs_and_only_essential_states_reach_action(self):
        lines=['A_ACCEPTED','A_LINEAR_PROGRESS:remaining_s=5','A_JIG_CONFIRMED:jig1','A_EPISODE_STARTED','A_PAUSED','A_RESUMED','A_STAGE_DONE:LOWER','A_TORQUE_OFF_CONFIRMED','A_DONE']
        goal=Goal('BUILD_A')
        with patch.object(self.module.subprocess,'Popen',return_value=Process(lines)):
            result=self.execute('arm2',goal)
        self.assertEqual(goal.messages,['RUNNING','PAUSED','RUNNING','IDLE'])
        self.assertEqual((result.success,result.message),(True,'IDLE'))
        self.assertEqual(self.server.logger.info.call_count,len(lines))
    def test_error_result_never_leaks_detailed_failure(self):
        goal=Goal('LOAD_A')
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['A_FAILED:private device detail'],1)):
            result=self.execute('arm3',goal)
        self.assertEqual(goal.messages,['RUNNING','ERROR'])
        self.assertEqual((result.success,result.message),(False,'ERROR'))
    def test_reset_original_job_is_not_claimed_completed(self):
        goal=Goal('BUILD_B')
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['B_RESET_DONE'],130)):
            result=self.execute('arm2',goal)
        self.assertEqual((result.success,result.message),(False,'ERROR'))
    def test_control_result_uses_confirmation_not_raw_ack_text(self):
        for command,feedback in [('EMER_STOP','PAUSED'),('RESTART','RUNNING'),('RESET','IDLE')]:
            goal=Goal(command)
            with patch.object(self.server,'invoke_control',return_value=True): result=self.execute('arm2',goal)
            self.assertEqual((result.success,result.message),(True,'IDLE'))
            self.assertEqual(goal.messages[-1],feedback)
    def test_control_failure_is_zero_error(self):
        goal=Goal('RESET')
        with patch.object(self.server,'invoke_control',return_value=False): result=self.execute('arm3',goal)
        self.assertEqual((result.success,result.message),(False,'ERROR'))

    def test_uppercase_jobs_run_existing_internal_commands(self):
        cases=[('arm2','BUILD_A','build_a','A_DONE'),('arm3','LOAD_B','load_b','B_DONE'),
               ('arm2','BUILD_LOAD_B','build_load_b','B_CHAIN_DONE'),('arm2','BUILD_SLIDE','slide_build','SLIDE_DONE'),('arm3','LOAD_SLIDE','slide_load','SLIDE_DONE')]
        for arm,command,internal,last in cases:
            with self.subTest(command=command),patch.object(self.module.subprocess,'Popen',return_value=Process([last])) as start:
                result=self.execute(arm,Goal(command))
                self.assertTrue(result.success);self.assertEqual(start.call_args.args[0][1],internal)

    def test_nonstandard_case_is_rejected_without_execution(self):
        for command in ('build_a','Build_A','build_load_b','emer_stop','estop','restart','reset','ESTOP','SLIDE_BUILD','SLIDE_LOAD'):
            self.assertFalse(self.server.goal_callback(Goal(command).request))
        self.assertFalse(self.server.job_lock.locked())

    def test_uppercase_does_not_allow_wrong_arm_or_unknown_commands(self):
        for command in ('LOAD_A','LOAD_SLIDE','BUILD_UNKNOWN'):
            self.assertFalse(self.server.goal_callback(Goal(command).request))
        self.assertFalse(self.server.job_lock.locked())
    def test_old_events_cannot_confirm_a_new_restart(self):
        info={'run_id':'a'*32}
        with patch.object(protocol,'job_events',return_value=[{'status':'A_RESUMED'}]):
            self.assertFalse(protocol.wait_control('/unused','restart',info,1,timeout=.001))
    def test_paused_job_confirms_only_its_estop_receipt(self):
        receipt={'job_id':'a'*32,'request_id':'b'*32,'command':'estop','state':'completed'}
        with patch.object(protocol,'control_state',return_value={'job_id':receipt['job_id'],'receipts':{receipt['request_id']:receipt}}):
            self.assertTrue(protocol.wait_control('/unused','estop',receipt,timeout=.1))
    def test_first_arm_completion_cannot_complete_whole_reset(self):
        info={'run_id':'a'*32}
        with patch.object(protocol,'job_events',return_value=[{'status':'B_ARM_RESET:arm2:RESET_DONE'},{'status':'B_ARM_RESET_DONE:arm2'}]):
            self.assertFalse(protocol.wait_control('/unused','reset',info,0,timeout=.01))

    def test_reset_requires_matching_completion_receipt_and_rejects_failure(self):
        receipt={'job_id':'a'*32,'request_id':'b'*32,'command':'reset'}
        for state,expected in [('completed',True),('failed',False)]:
            with patch.object(protocol,'control_state',return_value={'job_id':receipt['job_id'],'receipts':{receipt['request_id']:{**receipt,'state':state}}}):
                self.assertEqual(protocol.wait_control('/unused','reset',receipt,timeout=.1),expected)

    def test_estop_during_startup_is_sent_before_phase_metadata_exists(self):
        receipt={'job_id':'a'*32,'request_id':'b'*32,'command':'estop'}
        with patch.object(protocol,'control_state',return_value={'info':None,'job_id':'a'*32}),patch.object(protocol,'control_request',return_value=receipt) as command,patch.object(protocol,'wait_control',return_value=True) as wait:
            self.assertTrue(self.server.invoke_control('estop'))
            command.assert_called_once();self.assertEqual(wait.call_args.args[2],receipt)

    def test_both_arms_share_job_lock_but_controls_remain_available(self):
        other = self.module.ArmActionServer('arm3', self.server.job_lock, protocol,gate=self.server.gate)
        self.assertTrue(self.server.goal_callback(Goal('BUILD_A').request))
        self.assertFalse(other.goal_callback(Goal('LOAD_A').request))
        self.assertTrue(other.goal_callback(Goal('EMER_STOP').request))
        self.server.job_lock.release()
        self.server.dispatch_lock.close();self.server.dispatch_lock=None
        self.server.gate.retire(self.server.ticket);self.server.gate.release_control('EMER_STOP')
        self.assertTrue(other.goal_callback(Goal('LOAD_A').request))
        other.job_lock.release()
        other.dispatch_lock.close();other.dispatch_lock=None

    def test_wrong_arm_and_unknown_commands_rejected(self):
        for command in ('LOAD_A', 'invalid'):
            self.assertFalse(self.server.goal_callback(Goal(command).request))

    def test_launch_failure_releases_job_lock(self):
        goal = Goal('BUILD_A')
        with patch.object(self.module.subprocess, 'Popen', side_effect=OSError('unavailable')):
            result = self.execute('arm2', goal)
        self.assertEqual((result.success, result.message), (False, 'ERROR'))
        self.assertTrue(goal.aborted)
        self.assertFalse(self.server.job_lock.locked())

    def notices(self):
        return [event['status'] for path in (self.module.ROOT/'data/episode-cli-runs').glob('*.json') for event in json.loads(path.read_text())['events']]
    def test_rejected_goals_record_reason_without_executor(self):
        self.assertFalse(self.server.goal_callback(Goal('LOAD_A').request))
        self.server.job_lock.acquire()
        self.assertFalse(self.server.goal_callback(Goal('BUILD_B').request))
        self.server.job_lock.release()
        messages=self.notices()
        self.assertTrue(any('GOAL_REJECTED:LOAD_A:' in m for m in messages))
        self.assertTrue(any('GOAL_REJECTED:BUILD_B:BUSY' in m for m in messages))
        self.assertFalse((self.module.ROOT/'data/episode-cli-arm2.json').exists())
    def test_launch_failure_and_final_result_are_both_recorded(self):
        with patch.object(self.module.subprocess,'Popen',side_effect=OSError('cannot start')):
            self.execute('arm2',Goal('BUILD_B'))
        self.assertIn('ACTION_FAILED:BUILD_B:cannot start',self.notices())
        self.assertIn('ACTION_RESULT:BUILD_B:ERROR',self.notices())
    def test_warning_output_recorded_without_duplicating_cli_events(self):
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['runtime warning','A_DONE'])):
            self.execute('arm2',Goal('BUILD_A'))
        self.assertIn('EXECUTOR_OUTPUT:BUILD_A:runtime warning',self.notices())
        self.assertNotIn('A_DONE',self.notices())
    def test_control_without_active_job_has_explicit_reason(self):
        with patch.object(protocol,'control_state',return_value={'info':None}):
            self.assertFalse(self.server.invoke_control('restart'))
        self.assertIn('CONTROL_REJECTED:restart:NO_ACTIVE_JOB',self.notices())

    def test_reset_after_failed_process_exit_passes_request_endpoint_to_workcell_recovery(self):
        with patch.object(protocol,'control_state',side_effect=FileNotFoundError),patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='B_RESET_DONE\n',stderr='')) as command:
            self.assertTrue(self.server.invoke_control('reset'))
        self.assertIn('--recovery-arm',command.call_args.args[0]);self.assertIn(self.server.arm,command.call_args.args[0]);self.assertEqual(command.call_args.kwargs['timeout'],300)
    def test_idle_reset_does_not_report_success_without_safe_return_completion(self):
        with patch.object(protocol,'control_state',return_value={'info':None}),patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='B_FAILED:RESET:blocked\n',stderr='')):
            self.assertFalse(self.server.invoke_control('reset'))

    def test_combined_job_only_reports_idle_after_chain_completion(self):
        goal=Goal('BUILD_LOAD_B')
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['B_CHAIN_STARTED:build_load_b','B_DONE','B_CHAIN_LOAD_STARTED:build_load_b','B_DONE','B_CHAIN_DONE'])):
            result=self.execute('arm2',goal)
        self.assertEqual(result.message,'IDLE');self.assertEqual(goal.messages,['RUNNING','IDLE'])
    def test_combined_job_cannot_succeed_after_only_build_done(self):
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['B_DONE'])):
            result=self.execute('arm2',Goal('BUILD_LOAD_B'))
        self.assertEqual(result.message,'ERROR')

    def test_combined_job_reports_only_its_own_build_milestone_before_final_idle(self):
        for product,other in [('A','B'),('B','A')]:
            with self.subTest(product=product):
                goal=Goal('BUILD_LOAD_'+product)
                lines=[other+'_BUILD_DONE',product+'_BUILD_DONE',product+'_DONE',product+'_CHAIN_DONE']
                with patch.object(self.module.subprocess,'Popen',return_value=Process(lines)):
                    result=self.execute('arm2',goal)
                self.assertEqual(goal.messages,['RUNNING','BUILD_DONE','IDLE'])
                self.assertTrue(result.success)

    def test_pc_chain_preserves_command_identity_and_requires_final_load_result(self):
        for product in ('A','B'):
            goal=Goal('PC_BUILD_LOAD_'+product)
            lines=[product+'_BUILD_DONE',product+'_DONE',product+'_CHAIN_DONE']
            with patch.object(self.module.subprocess,'Popen',return_value=Process(lines)) as launch:
                result=self.execute('arm2',goal)
            self.assertTrue(result.success);self.assertIn('pc_build_load_'+product.lower(),launch.call_args.args[0])
            self.assertEqual(goal.messages,['RUNNING','BUILD_DONE','IDLE'])
            with patch.object(self.module.subprocess,'Popen',return_value=Process([product+'_BUILD_DONE'])):
                result=self.execute('arm2',Goal('PC_BUILD_LOAD_'+product))
            self.assertFalse(result.success)

    def test_build_milestone_does_not_complete_the_combined_job(self):
        goal=Goal('BUILD_LOAD_A')
        with patch.object(self.module.subprocess,'Popen',return_value=Process(['A_BUILD_DONE'])):
            result=self.execute('arm2',goal)
        self.assertEqual(goal.messages,['RUNNING','BUILD_DONE','ERROR'])
        self.assertFalse(result.success)
