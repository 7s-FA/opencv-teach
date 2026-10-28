"""New multi-event schedules, using fake hardware and manually released I/O."""
import io,json,threading,time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.test_wpf_bridge import AsyncClientStorageTests as ClientFixture,BridgeTests as BridgeFixture
from tests.test_gui_admission import GuiAdmissionTests as GuiFixture
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach import pi_execution as gui

class RequestProofTests(TestCase):
    setUpClass=classmethod(ClientFixture.setUpClass.__func__)
    setUp=ClientFixture.setUp
    def test_restart_keeps_pre_stop_proof_across_journal_delay(self):
        current=['before-local-stop']
        self.client.health.envelope=lambda *args:current[0]
        self.client.send_goal('RESTART',request_id='a'*32)
        current[0]='after-local-stop-and-reconnect'
        self.writes[0][1](None)
        goal=self.client._client.send_goal_async.call_args.args[0]
        self.assertEqual(goal.command,'before-local-stop')

    def test_pi_fence_rejects_restart_saved_before_a_second_local_stop(self):
        from supervision_session import SupervisionHealth
        from supervision_gate import SupervisionGate
        h=object.__new__(SupervisionHealth);h.lock=threading.RLock();h.gate=SupervisionGate()
        h.epoch='pi';now=time.monotonic();h.seen={'c'*32:now};h.challenges={'before':now,'after':now}
        h.fences={'before':(0,'job',0,0),'after':(1,'job',1,0)}
        nonce=['before']
        self.client.health.envelope=lambda cmd,key,op:json.dumps(dict(command=cmd,request_id=key,client_id='c'*32,epoch='pi',nonce=nonce[0]))
        self.client.send_goal('RESTART',request_id='a'*32)
        h.gate.stop_active();nonce[0]='after';self.writes[0][1](None)
        sent=json.loads(self.client._client.send_goal_async.call_args.args[0].command)
        self.assertFalse(h.accepts(sent));self.assertFalse(h.control_fence(sent,{'job_id':'job','stop_generation':1}))
        fresh=json.loads(self.client.health.envelope('RESTART','b'*32,'execute'))
        self.assertTrue(h.accepts(fresh));self.assertTrue(h.control_fence(fresh,{'job_id':'job','stop_generation':1}))

    def test_local_console_also_freezes_proof_before_synchronous_fsync(self):
        current=['old'];self.client.health.envelope=lambda *args:current[0]
        self.client.journal_writer=None
        with patch.object(self.client.journal,'save',side_effect=lambda:current.__setitem__(0,'recovered')):
            self.client.send_goal('RESET',request_id='a'*32)
        self.assertEqual(self.client._client.send_goal_async.call_args.args[0].command,'old')

class ReceiptProofTests(TestCase):
    setUp=BridgeFixture.setUp
    connect=BridgeFixture.connect
    request=BridgeFixture.request
    def test_original_proof_survives_receipt_delay_and_server_restart(self):
        writes=[];arm=self.arms['arm2'];current=['old-pi-epoch']
        arm.health.envelope=lambda *args:current[0]
        arm.send_goal=Mock(return_value=__import__('concurrent.futures',fromlist=['Future']).Future())
        self.service.writer=lambda path,value,done:writes.append(done)
        self.service.submit(self.request('RESET'))
        current[0]='new-pi-epoch';writes[0](None)
        self.assertEqual(arm.send_goal.call_args.kwargs.get('envelope'),'old-pi-epoch')

class GuiConnectionTests(TestCase):
    setUp=GuiFixture.setUp
    def test_ready_preparation_plus_peer_disconnect_never_launches_ssh(self):
        entered=threading.Event();release=threading.Event()
        def claim(*args):entered.set();release.wait(1)
        self.app.remote.rpc.side_effect=claim
        process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO(),wait=Mock(return_value=0),poll=Mock(return_value=0))
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen',return_value=process) as launch:
            job=gui.PiExecution(self.app)
            try:
                self.assertTrue(entered.wait(1));self.manager.apps['arm3'].remote.error='disconnected'
            finally:release.set()
            job.prepare_thread.join(1);job.poll()
            if job.thread:job.thread.join(1)
            if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
            job.poll()
        launch.assert_not_called();self.assertFalse(job.busy)

    def test_connection_loss_between_poll_and_ssh_worker_is_rechecked(self):
        with patch.object(gui.PiExecution,'poll'):
            job=gui.PiExecution(self.app)
        job.prepare_thread.join(1)
        self.app.remote.stop.set()
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen') as launch:
            job.run();job.poll()
        launch.assert_not_called();self.assertFalse(job.busy)

    def test_disconnect_then_recovery_during_preparation_never_auto_starts(self):
        entered=threading.Event();release=threading.Event()
        def claim(method,*args):
            if method=='claim_episode':entered.set();release.wait(1)
        self.app.remote.rpc.side_effect=claim
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen') as launch:
            job=gui.PiExecution(self.app)
            try:
                self.assertTrue(entered.wait(1));self.app.remote.error='offline';job.poll()
                self.app.remote.error=None
            finally:release.set()
            job.prepare_thread.join(1);job.cancel_worker.join(1);job.poll()
        launch.assert_not_called();self.assertTrue(job.cancelled.is_set());self.assertFalse(job.busy)

class LinearDispatchTests(TestCase):
    def runner(self,event):
        control=cli.Control();linear=FakeLinear();link=FakeLink()
        runner=cli.Runner(link,event,control,10,linear=linear)
        return runner,control,linear
    def test_stop_while_start_notice_is_saved_defers_new_linear_target(self):
        runner,control,linear=self.runner(lambda _:None)
        def emit(event):
            if event.startswith('LINEAR_CHECK:'):control.command('estop')
        runner.emit=emit
        self.assertFalse(runner.start_linear(100.,defer_pause=True))
        self.assertFalse(linear.calls)

    def test_normal_start_waits_for_explicit_restart_then_dispatches_once(self):
        runner,control,linear=self.runner(lambda _:None);events=[]
        def emit(event):
            events.append(event)
            if event.startswith('LINEAR_CHECK:'):control.command('estop')
            elif event=='PAUSED':control.command('restart')
        runner.emit=emit;original=linear.call
        def call(op,**args):events.append(op);return original(op,**args)
        linear.call=call
        self.assertTrue(runner.start_linear(100.))
        self.assertLess(events.index('RESUMED'),events.index('ensure'))
        self.assertEqual(sum(op=='ensure' for op,_ in linear.calls),1)

    def test_shutdown_during_start_notice_does_not_move_linear(self):
        runner,control,linear=self.runner(lambda _:None)
        runner.emit=lambda _:control.shutdown.set()
        with self.assertRaises(cli.ShutdownRequested):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)

    def test_deadline_during_start_notice_does_not_move_linear(self):
        runner,control,linear=self.runner(lambda _:None)
        runner.emit=lambda _:setattr(runner,'deadline',0)
        with self.assertRaisesRegex(RuntimeError,'제한시간'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)

    def test_peer_lease_lost_during_notice_blocks_linear_despite_fresh_active_arm(self):
        runner,control,linear=self.runner(lambda _:None)
        runner.peer=SimpleNamespace(error=None,state=Mock(side_effect=RuntimeError('peer lease changed')))
        with self.assertRaisesRegex(RuntimeError,'peer lease changed'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)

    def test_stop_arriving_during_peer_check_still_defers_linear(self):
        runner,control,linear=self.runner(lambda _:None)
        def read():control.command('estop');return None
        runner.peer=SimpleNamespace(error=None,state=read)
        self.assertFalse(runner.start_linear(100.,defer_pause=True))
        self.assertFalse(linear.calls)

    def test_finished_inspection_stop_restart_returns_linear_exactly_once(self):
        runner,control,linear=self.runner(lambda _:None);stops=[]
        runner.finished_return_step='finished';runner.inspection_passed={'finished'};runner.inspection_due=[]
        def emit(event):
            if event.startswith('LINEAR_CHECK:') and not stops:stops.append(True);control.command('estop')
            elif event=='PAUSED':control.command('restart')
        runner.emit=emit
        runner.start_finished_return();self.assertFalse(runner.linear_return_started);self.assertFalse(linear.calls)
        runner.await_pause();runner.start_finished_return();runner.start_finished_return()
        self.assertTrue(runner.linear_return_started)
        self.assertEqual(sum(op=='ensure' for op,_ in linear.calls),1)
    def test_disconnect_while_start_notice_is_saved_blocks_linear(self):
        runner,control,linear=self.runner(lambda _:None)
        def emit(event):
            if event.startswith('LINEAR_CHECK:'):linear.error='lost connection'
        runner.emit=emit
        with self.assertRaisesRegex(RuntimeError,'lost connection'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)
