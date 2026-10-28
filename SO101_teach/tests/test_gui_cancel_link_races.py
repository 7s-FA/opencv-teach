"""Cancellation/connection replacement races; all transports are fake."""
import io,threading
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.test_gui_result_protocol import ResultProtocolTests as Fixture
from so101_teach import pi_execution as gui

class CancelLinkRaceTests(TestCase):
    setUp=Fixture.setUp
    job=Fixture.job
    result=Fixture.result
    run_stream=Fixture.run_stream

    def test_stop_after_received_success_before_poll_preserves_completion(self):
        job=self.job()
        with patch.object(job,'poll'):self.run_stream(job,[self.result(job,0)],0)
        job.cancel()
        if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
        job.poll()
        self.assertEqual(job.record['state'],'done')
        self.app.remote.rpc.assert_not_called()

    def test_stop_after_received_reset_does_not_cancel_idle_release(self):
        job=self.job()
        with patch.object(job,'poll'):self.run_stream(job,[self.result(job,130)],130)
        job.cancel()
        if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
        job.poll()
        self.assertEqual(job.record['state'],'stopped')
        self.assertFalse(job.cancelled.is_set())
        self.app.remote.rpc.assert_not_called()

    def test_extra_stop_during_failure_hold_preserves_failure_and_single_hold(self):
        job=self.job();entered=threading.Event();release=threading.Event()
        def hold(*args):entered.set();release.wait(2)
        self.app.remote.rpc.side_effect=hold
        process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO('A_FAILED:original fault\n'+self.result(job,1)+'\n'),wait=Mock(return_value=1),poll=Mock(return_value=1))
        with patch.object(gui,'command_args',return_value=['fake']),patch.object(gui.subprocess,'Popen',return_value=process):
            worker=threading.Thread(target=job.run);worker.start()
            try:
                self.assertTrue(entered.wait(1));job.cancel();job.poll()
                self.assertTrue(job.busy)
            finally:
                release.set();worker.join(1)
                if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
            job.poll()
        self.assertEqual(job.record['state'],'failed')
        self.assertEqual(job.record['error'],'A_FAILED:original fault')
        self.app.remote.rpc.assert_called_once_with('command',{'action':'hold'})

    def replacement(self):
        return SimpleNamespace(config={},rpc=Mock(),error=None,stop=threading.Event(),execution_owner=object(),bundle_hash='new bundle')

    def test_replacement_before_launch_blocks_ssh_and_leaves_new_owner_intact(self):
        job=self.job();old=self.app.remote;new=self.replacement();self.app.remote=new
        with patch.object(gui,'command_args',return_value=['fake']),patch.object(gui.subprocess,'Popen',side_effect=AssertionError('must not launch')) as launch:job.run()
        job.poll()
        launch.assert_not_called();new.rpc.assert_not_called()
        self.assertIsNone(old.execution_owner);self.assertIsNotNone(new.execution_owner)
        self.assertEqual(new.bundle_hash,'new bundle');self.assertEqual(job.record['state'],'failed')

    def test_replacement_during_stream_failure_never_holds_new_connection(self):
        job=self.job();old=self.app.remote;new=self.replacement()
        def output():
            self.app.remote=new
            raise OSError('old SSH lost')
            yield
        process=SimpleNamespace(stdin=io.StringIO(),stdout=output(),wait=Mock(return_value=1),poll=Mock(return_value=1))
        with patch.object(gui,'command_args',return_value=['fake']),patch.object(gui.subprocess,'Popen',return_value=process):job.run()
        job.poll()
        new.rpc.assert_not_called();old.rpc.assert_called_once_with('command',{'action':'hold'})
        self.assertIsNone(old.execution_owner);self.assertEqual(new.bundle_hash,'new bundle')

    def test_replacement_before_cancel_holds_only_original_connection(self):
        job=self.job();old=self.app.remote;new=self.replacement();self.app.remote=new
        job.cancel();job.cancel_worker.join(1);job.finish(1)
        new.rpc.assert_not_called();old.rpc.assert_called_once_with('command',{'action':'hold'})
        self.assertIsNone(old.execution_owner);self.assertEqual(job.record['state'],'stopped')

    def test_replacement_during_first_claim_never_claims_new_peer(self):
        entered=threading.Event();release=threading.Event();peer=self.manager.apps['arm3'];old=peer.remote;new=self.replacement()
        def claim(*args):entered.set();release.wait(2)
        self.app.remote.rpc.side_effect=claim
        with patch.object(gui.PiExecution,'poll'):job=gui.PiExecution(self.app)
        try:
            self.assertTrue(entered.wait(1));peer.remote=new
        finally:release.set();job.prepare_thread.join(1)
        with patch.object(gui.subprocess,'Popen') as launch:job.poll()
        new.rpc.assert_not_called();launch.assert_not_called()
        self.assertFalse(job.busy);self.assertIsNone(old.execution_owner)

    def test_changed_lease_on_same_link_blocks_old_snapshot(self):
        job=self.job();link=self.app.remote
        link.lease='new lease';link.server_port=123;link.token='token';link.instance='instance'
        job.request['links']={'arm2':{'lease':'old lease','port':123,'token':'token','instance':'instance'}}
        self.app.profile={'robot_id':'arm2'}
        with patch.object(gui,'command_args',return_value=['fake']),patch.object(gui.subprocess,'Popen',side_effect=AssertionError('must not launch')) as launch:job.run()
        job.poll();launch.assert_not_called();link.rpc.assert_not_called();self.assertEqual(job.record['state'],'failed')

    def test_release_after_result_preserves_success_and_releases_requested_arm(self):
        self.app.profile={'robot_id':'arm2'};self.app.session=SimpleNamespace(request=Mock())
        job=self.job()
        with patch.object(job,'poll'):self.run_stream(job,[self.result(job,0)],0)
        job.cancel('release');job.poll()
        self.assertEqual(job.record['state'],'done')
        self.app.remote.rpc.assert_not_called();self.app.session.request.assert_called_once_with('release')

    def test_deferred_release_does_not_reach_replaced_session(self):
        self.app.profile={'robot_id':'arm2'};self.app.session=SimpleNamespace(request=Mock())
        job=self.job();old=self.app.session
        with patch.object(job,'poll'):self.run_stream(job,[self.result(job,0)],0)
        job.cancel('release');self.app.session=SimpleNamespace(request=Mock());job.poll()
        old.request.assert_not_called();self.app.session.request.assert_not_called()
        self.assertIn('연결이 변경',self.app.notice.call_args[0][0])

    def test_disconnect_removing_link_does_not_crash_finalization(self):
        job=self.job();old=self.app.remote;self.app.remote=None
        with patch.object(gui.subprocess,'Popen') as launch:job.run()
        job.poll();launch.assert_not_called();self.assertFalse(job.busy)
        self.assertIsNone(old.execution_owner);self.assertEqual(job.record['state'],'failed')

    def test_config_changed_during_preparation_blocks_launch(self):
        job=self.job();self.app.remote.config['host']='different-pi'
        with patch.object(gui.subprocess,'Popen') as launch:job.run()
        job.poll();launch.assert_not_called();self.assertEqual(job.record['state'],'failed')
