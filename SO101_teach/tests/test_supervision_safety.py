"""Regression checks use fake arms/ROS and local IPC, never device endpoints."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
from tests.test_action_contract import ActionContractTests as ActionFixture,Goal,Process,protocol
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from tests.test_chain_parallel_start import ChainParallelStartTests as ChainFixture
from supervision_gate import SupervisionGate
import episode_recovery


class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ActionFixture.setUpClass()
    def setUp(self):
        self.f=ActionFixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.server=self.f.server;self.goals={}
    def goal(self,command):
        if command not in self.goals:self.goals[command]=Goal(command)
        return self.goals[command]

    def test_stop_before_child_launch_cancels_reserved_job_even_after_stop_callback_wait(self):
        self.assertTrue(self.server.goal_callback(self.goal('BUILD_A').request))
        self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
        with patch.object(self.f.module.subprocess,'Popen') as start:
            result=self.server.execute_callback(self.goal('BUILD_A'))
            self.assertFalse(result.success);start.assert_not_called()
        self.assertTrue(self.server.execute_callback(self.goal('EMER_STOP')).success)
        self.assertFalse(self.server.job_lock.locked())

    def test_other_arm_stop_latches_the_same_pending_job(self):
        other=self.f.module.ArmActionServer('arm3',self.server.job_lock,protocol,gate=self.server.gate)
        self.assertTrue(self.server.goal_callback(self.goal('BUILD_A').request))
        self.assertTrue(other.goal_callback(self.goal('EMER_STOP').request))
        with patch.object(self.f.module.subprocess,'Popen') as start:
            self.assertFalse(self.server.execute_callback(self.goal('BUILD_A')).success);start.assert_not_called()
        self.assertTrue(other.execute_callback(self.goal('EMER_STOP')).success)

    def test_duplicate_controls_are_bounded_but_every_emergency_signals_pipe(self):
        ticket=self.server.gate.reserve()
        self.assertTrue(self.server.goal_callback(self.goal('RESET').request))
        for command in ('RESET','RESTART','CLEAR_ERROR'):
            self.assertFalse(self.server.goal_callback(Goal(command).request))
        self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
        self.assertEqual(os.read(ticket.read_fd,1),b'!')
        self.assertFalse(self.server.goal_callback(self.goal('EMER_STOP').request))
        self.assertEqual(os.read(ticket.read_fd,1),b'!')
        self.assertEqual(len(self.server.gate.controls),2)

    def test_emergency_signal_does_not_wait_for_blocked_ordinary_control(self):
        ticket=self.server.gate.reserve();entered=threading.Event();release=threading.Event()
        self.assertTrue(self.server.goal_callback(self.goal('RESET').request))
        def blocked(command):entered.set();release.wait(2);return False
        with patch.object(self.server,'invoke_control',side_effect=blocked):
            thread=threading.Thread(target=self.server.execute_callback,args=(self.goal('RESET'),));thread.start()
            try:
                self.assertTrue(entered.wait(1));self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
                self.assertEqual(os.read(ticket.read_fd,1),b'!')
                self.assertTrue(thread.is_alive())
            finally:release.set();thread.join(2)

    def test_old_cleanup_cannot_remove_the_next_ticket(self):
        old=self.server.gate.reserve();self.server.gate.retire(old)
        new=self.server.gate.reserve();self.server.gate.retire(old)
        self.assertIs(self.server.gate.active,new)
        self.server.gate.stop_active();self.assertEqual(os.read(new.read_fd,1),b'!')

    def test_stop_cancels_accepted_idle_reset_before_it_can_launch(self):
        self.assertTrue(self.server.goal_callback(self.goal('RESET').request))
        self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
        with patch.object(self.server,'invoke_control') as invoke:
            self.assertFalse(self.server.execute_callback(self.goal('RESET')).success)
            invoke.assert_not_called()
        self.assertFalse(self.server.goal_callback(self.goal('RESTART').request))

    def test_stop_during_idle_detection_prevents_recovery_launch(self):
        self.assertTrue(self.server.goal_callback(self.goal('RESET').request))
        def state(root):
            self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
            raise FileNotFoundError
        with patch.object(protocol,'control_state',side_effect=state),patch.object(self.f.module.subprocess,'run') as launch:
            self.assertFalse(self.server.execute_callback(self.goal('RESET')).success)
            launch.assert_not_called()

    def test_stop_suppresses_late_success_of_inflight_restart(self):
        self.assertTrue(self.server.goal_callback(self.goal('RESTART').request))
        def restart(command):
            self.assertTrue(self.server.goal_callback(self.goal('EMER_STOP').request))
            return True
        goal=self.goal('RESTART')
        with patch.object(self.server,'invoke_control',side_effect=restart):
            self.assertFalse(self.server.execute_callback(goal).success)
        self.assertEqual(goal.messages[-1],'ERROR')

    def test_reserved_idle_reset_never_redirects_to_a_newly_started_job(self):
        with patch.object(cli,'send_control',side_effect=AssertionError('must not target a different active job')),patch.object(episode_recovery,'run',return_value=0) as recovery:
            self.assertEqual(cli.main(['reset','--no-ros','--supervision-job-id','a'*32,'--stop-fd','123']),0)
        self.assertEqual(recovery.call_args.kwargs['supervision_job_id'],'a'*32)
        self.assertEqual(recovery.call_args.kwargs['stop_fd'],123)

    def test_idle_reset_inherits_stop_pipe_and_job_identity(self):
        def run(args,**kwargs):
            ticket=self.server.gate.active
            self.assertIn(ticket.job_id,args);self.assertEqual(kwargs['pass_fds'],(ticket.read_fd,))
            self.server.gate.stop_active();self.assertEqual(os.read(ticket.read_fd,1),b'!')
            return type('Result',(),dict(returncode=0,stdout='A_RESET_DONE\n',stderr=''))()
        with patch.object(protocol,'control_state',side_effect=FileNotFoundError),patch.object(self.f.module.subprocess,'run',side_effect=run):
            self.assertTrue(self.server.invoke_control('reset'))
        self.assertIsNone(self.server.gate.active)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.patch=patch.object(cli,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.control=cli.Control();self.control.info={'arm':'arm2','run_id':'a'*32}
        self.done=threading.Event();self.thread=threading.Thread(target=cli.control_server,args=(self.control,self.done))
        self.thread.start();self.addCleanup(self.stop_server)
        for _ in range(200):
            try:protocol.control_state(self.root);break
            except (FileNotFoundError,ConnectionRefusedError):pass
            time.sleep(.001)
        self.assertTrue(cli.control_path().exists())
    def stop_server(self):self.done.set();self.thread.join(2)
    def request(self,command,**kwargs):return protocol.control_request(self.root,command,**kwargs)

    def test_delayed_old_log_cannot_complete_pending_restart(self):
        self.control.paused=True;receipt=self.request('restart')
        with patch.object(protocol,'job_events',side_effect=AssertionError('logs must not confirm controls')):
            self.assertFalse(protocol.wait_control(self.root,'restart',receipt,timeout=.01))
            self.control.paused=False;self.control.complete('restart')
            self.assertTrue(protocol.wait_control(self.root,'restart',receipt,timeout=.1))

    def test_chain_phase_run_id_changes_do_not_break_stop_confirmation(self):
        receipt=self.request('estop')
        self.control.info={'arm':'arm3','run_id':'b'*32}
        self.control.paused=True;self.control.complete('estop')
        self.assertTrue(protocol.wait_control(self.root,'estop',receipt,timeout=.1))

    def test_wrong_job_and_reused_request_for_other_command_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'JOB_CHANGED'):self.request('estop',job_id='f'*32)
        self.assertFalse(self.control.pause.is_set())
        receipt=self.request('estop',request_id='c'*32)
        with self.assertRaisesRegex(ValueError,'REQUEST_CHANGED'):self.request('restart',request_id=receipt['request_id'])

    def test_request_retry_is_idempotent_and_does_not_repeat_hold(self):
        self.control.emergency=Mock();first=self.request('estop',request_id='d'*32)
        self.assertEqual(self.request('estop',request_id='d'*32),first)
        self.control.emergency.assert_called_once()

    def test_new_estop_cancels_pending_restart(self):
        self.control.paused=True;restart=self.request('restart');stop=self.request('estop')
        self.assertFalse(protocol.wait_control(self.root,'restart',restart,timeout=.1))
        self.assertFalse(protocol.wait_control(self.root,'estop',stop,timeout=.01))
        self.assertTrue(self.control.confirm_estop([FakeLink().state()]))
        self.assertTrue(protocol.wait_control(self.root,'estop',stop,timeout=.1))
        self.assertFalse(self.control.resume.is_set())

    def test_many_controls_keep_inspection_bounded_and_old_receipt_still_verifiable(self):
        self.control.paused=True
        first=None
        for i in range(40):
            receipt=self.request('estop',request_id=f'{i:032x}')
            if first is None:first=receipt
        state=protocol.control_state(self.root)
        self.assertLess(len(json.dumps(state).encode()),8192)
        self.assertEqual(len(state['receipts']),16)
        self.assertFalse(protocol.wait_control(self.root,'estop',first,timeout=.01))
        self.assertTrue(self.control.confirm_estop([FakeLink().state()]))
        self.assertTrue(protocol.wait_control(self.root,'estop',first,timeout=.1))

    def test_completion_receipt_survives_executor_socket_exit(self):
        self.control.paused=True;receipt=self.request('reset');self.control.complete('reset');self.stop_server()
        self.assertTrue(protocol.wait_control(self.root,'reset',receipt,timeout=.1))
        receipt['request_id']='e'*32
        self.assertFalse(protocol.wait_control(self.root,'reset',receipt,timeout=.01))

    def test_real_pause_resume_path_completes_only_matching_requests(self):
        link=FakeLink();link.mode='HOLD';runner=cli.Runner(link,lambda event:None,self.control,5,linear=FakeLinear())
        stop=self.request('estop');errors=[]
        def run():
            try:runner.check()
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(protocol.wait_control(self.root,'estop',stop,timeout=1))
            restart=self.request('restart')
            self.assertTrue(protocol.wait_control(self.root,'restart',restart,timeout=1))
        finally:thread.join(2)
        self.assertFalse(errors);self.assertFalse(thread.is_alive());self.assertFalse(self.control.paused)

    def test_pre_socket_stop_signal_is_consumed_before_preparation(self):
        gate=SupervisionGate();ticket=gate.reserve();gate.stop_active();done=threading.Event()
        read_fd=os.dup(ticket.read_fd)
        worker=self.control.configure_supervision(ticket.job_id,read_fd,done)
        try:
            self.assertTrue(self.control.pause.is_set());self.assertEqual(self.control.job_id,ticket.job_id)
            self.control.pause.clear();gate.stop_active()
            self.assertTrue(self.control.pause.wait(1))
        finally:done.set();gate.retire(ticket);worker.join(2)
        self.assertFalse(worker.is_alive())


class ResetReceiptTests(unittest.TestCase):
    def test_both_safe_arrivals_are_required_before_reset_receipt_completion(self):
        fixture=ChainFixture();fixture.setUp();self.addCleanup(fixture.doCleanups)
        control=fixture.control;control.paused=True;control.pause.set()
        key='d'*32
        control.command('control '+json.dumps({'job_id':control.job_id,'request_id':key,'command':'reset','stop_generation':control.stop_generation}))
        # Runner.await_pause consumes RESET before the main handler calls recovery.
        control.reset.clear();control.pause.clear();control.paused=False
        original=fixture.links['arm3'].rpc
        def call(method,args=None):
            if method=='command' and args['action']=='move':self.assertEqual(control.receipts[key]['state'],'pending')
            return original(method,args)
        with patch.object(fixture.links['arm3'],'rpc',side_effect=call):
            episode_recovery.reset_pair(cli,fixture.shared['records'],fixture.links,fixture.linear,control,lambda event:None,
                active=None,first='arm2',startup=False,persist=False)
        self.assertEqual(control.receipts[key]['state'],'completed')
