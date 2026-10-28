"""Completion/disconnection and peer lifecycle combinations omitted before."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.test_gui_admission import GuiAdmissionTests as GuiFixture
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from tests.test_gui_pi_execution import BlockingInput
from so101_teach import pi_execution as gui,pi_execution_receiver as receiver

class CompletionTests(TestCase):
    setUp=GuiFixture.setUp
    def job(self):
        with patch.object(gui.PiExecution,'poll'):job=gui.PiExecution(self.app)
        job.prepare_thread.join(1);job.thread=object()
        for app in job.apps:app.remote.rpc.reset_mock()
        return job
    def drain(self,job):
        job.poll()
        if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
        if job.busy:job.finish(0)
    def test_done_then_peer_disconnect_does_not_cancel_completed_job(self):
        job=self.job();job.completion_received.set();job.events.put(('line','A_DONE'))
        job.events.put(('result',0))
        self.manager.apps['arm3'].remote.error='peer disconnected after completion'
        self.drain(job)
        self.assertEqual(job.record['state'],'done');self.assertFalse(job.cancelled.is_set())
        self.app.remote.rpc.assert_not_called()

    def test_done_behind_large_ui_event_backlog_survives_disconnect(self):
        job=self.job()
        for _ in range(150):job.events.put(('line','A_INSPECTION_COUNT:1/2'))
        job.completion_received.set();job.events.put(('line','A_DONE'))
        job.events.put(('result',0))
        self.manager.apps['arm3'].remote.stop.set()
        job.poll();self.assertTrue(job.busy);self.assertFalse(job.cancelled.is_set())
        job.poll();self.assertFalse(job.busy);self.assertEqual(job.record['state'],'done')
        self.app.remote.rpc.assert_not_called()
    def test_user_cancel_before_done_still_records_stopped(self):
        job=self.job();job.cancel();job.cancel_worker.join(1)
        job.completion_received.set();job.events.put(('line','A_DONE'));job.events.put(('result',0));self.drain(job)
        self.assertEqual(job.record['state'],'stopped')
    def test_disconnect_without_done_cancels_and_never_reports_success(self):
        job=self.job();self.app.remote.error='offline';job.events.put(('exit',0));self.drain(job)
        self.assertEqual(job.record['state'],'stopped');self.assertTrue(job.cancelled.is_set())
    def test_partial_done_suffix_and_zero_exit_are_not_episode_success(self):
        for line in ('A_EPISODE_DONE','A_SAFE_RETURN_DONE'):
            with self.subTest(line=line):
                job=self.job();job.events.put(('line',line));job.events.put(('exit',0));self.drain(job)
                self.assertEqual(job.record['state'],'failed')
    def test_local_reset_done_is_stopped_not_full_episode_success(self):
        job=self.job();job.events.put(('line','A_RESET_DONE'));job.events.put(('result',130));self.drain(job)
        self.assertEqual(job.record['state'],'stopped')

class PeerLifecycleTests(TestCase):
    def runner(self,peer):
        linear=FakeLinear();runner=cli.Runner(FakeLink(),lambda _:None,cli.Control(),10,peer=peer,linear=linear)
        return runner,linear
    def test_peer_reports_hardware_fault_before_heartbeat_error_blocks_linear(self):
        state=FakeLink().state();state['error']='motor disconnected'
        runner,linear=self.runner(SimpleNamespace(error=None,state=lambda:state))
        with self.assertRaisesRegex(RuntimeError,'motor disconnected'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)
    def test_previously_live_peer_unexpectedly_closed_blocks_linear(self):
        state=FakeLink().state();state['running']=False
        runner,linear=self.runner(SimpleNamespace(error=None,state=lambda:state,initial_inactive_follower=None))
        with self.assertRaisesRegex(RuntimeError,'연결 종료'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)
    def test_preexisting_verified_off_peer_remains_usable(self):
        state=FakeLink().state();state['running']=False
        runner,linear=self.runner(SimpleNamespace(error=None,state=lambda:deepcopy(state),initial_inactive_follower=deepcopy(state)))
        self.assertTrue(runner.start_linear(100.,defer_pause=True));self.assertEqual(len(linear.calls),1)

    def test_running_peer_with_stale_or_incomplete_telemetry_blocks_linear(self):
        for problem in ('stale','calibration','missing_joint'):
            with self.subTest(problem=problem):
                state=FakeLink().state()
                if problem=='stale':state['latest']['monotonic']-=5
                elif problem=='calibration':state['latest']['calibration_matches']=False
                else:state['latest']['telemetry'].pop('0')
                runner,linear=self.runner(SimpleNamespace(error=None,state=lambda:state))
                with self.assertRaisesRegex(RuntimeError,'최신 모터 상태'):runner.start_linear(100.,defer_pause=True)
                self.assertFalse(linear.calls)

    def test_peer_sample_aging_during_active_read_blocks_linear(self):
        now=[10.]
        with patch.object(cli.time,'monotonic',side_effect=lambda:now[0]):
            peer=SimpleNamespace(error=None,state=lambda:FakeLink().state())
            runner,linear=self.runner(peer);reads=[]
            def read_active():
                reads.append(True)
                if len(reads)>1:now[0]+=.6
                return FakeLink().state()
            runner.state=read_active
            with self.assertRaisesRegex(RuntimeError,'최신 모터 상태'):runner.start_linear(100.,defer_pause=True)
        self.assertFalse(linear.calls)
    def test_gui_borrowed_link_cannot_lose_a_previously_live_follower(self):
        original={name:getattr(cli,name) for name in ('Link','Control','Status','load_job')}
        try:
            receiver.install_adapters(cli,{'apply_jig':True},None,BlockingInput());link=cli.Link('arm2')
            with patch.object(link,'post',side_effect=[{'running':True,'stop_generation':0},None]):
                link.state()
                with self.assertRaisesRegex(RuntimeError,'팔로워 상태'):link.state()
        finally:
            for name,value in original.items():setattr(cli,name,value)
