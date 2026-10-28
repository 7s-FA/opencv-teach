"""Real GUI reader with fake SSH streams, covering terminal framing and exit codes."""
import io,json,threading,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.test_gui_admission import GuiAdmissionTests as Fixture
from so101_teach import pi_execution as gui
from so101_teach import pi_execution_receiver as receiver
from tests.test_episode_cli import cli

class ResultProtocolTests(TestCase):
    setUp=Fixture.setUp
    def job(self):
        with patch.object(gui.PiExecution,'poll'):job=gui.PiExecution(self.app)
        job.prepare_thread.join(1);job.thread=object()
        job.request.setdefault('execution_id','a'*32)
        for app in job.apps:app.remote.rpc.reset_mock()
        return job
    def result(self,job,code,**change):
        return 'GUI_RESULT:'+json.dumps(dict(execution_id=job.request['execution_id'],exit_code=code,**change))
    def run_stream(self,job,lines,code):
        process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO('\n'.join(lines)+'\n'),wait=Mock(return_value=code),poll=Mock(return_value=code))
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen',return_value=process):job.run()
        job.poll()
        return job.record['state']
    def test_confirmed_reset_exit_130_preserves_stop_without_late_hold(self):
        job=self.job()
        self.assertEqual(self.run_stream(job,['A_RESET_DONE',self.result(job,130)],130),'stopped')
        self.app.remote.rpc.assert_not_called()
    def test_reset_result_survives_trailing_diagnostic_output(self):
        job=self.job()
        self.assertEqual(self.run_stream(job,['A_RESET_DONE',self.result(job,130),'publisher closed'],130),'stopped')
        self.app.remote.rpc.assert_not_called()
    def test_raw_done_line_and_zero_exit_cannot_forge_completion(self):
        job=self.job()
        self.assertEqual(self.run_stream(job,['A_STEP_DONE:1/2:name','A_DONE','A_FAILED:camera error'],0),'failed')
    def test_result_for_another_execution_cannot_complete_this_job(self):
        job=self.job();other='b'*32 if job.request['execution_id']!='b'*32 else 'c'*32
        message='GUI_RESULT:'+json.dumps(dict(execution_id=other,exit_code=0))
        self.assertEqual(self.run_stream(job,['A_DONE',message],0),'failed')
    def test_confirmed_success_survives_trailing_diagnostic_output(self):
        job=self.job()
        self.assertEqual(self.run_stream(job,['A_DONE',self.result(job,0),'publisher closed'],0),'done')
        self.app.remote.rpc.assert_not_called()

    def test_confirmed_failure_then_disconnect_preserves_failure_reason(self):
        job=self.job()
        with patch.object(job,'poll'):
            self.run_stream(job,['A_FAILED:motor fault',self.result(job,1)],1)
        self.manager.apps['arm3'].remote.error='peer disconnected after failure'
        job.poll()
        if getattr(job,'cancel_worker',None):job.cancel_worker.join(1)
        if job.busy:job.finish(1)
        self.assertEqual(job.record['state'],'failed');self.assertFalse(job.cancelled.is_set())
        self.assertEqual(job.record['error'],'A_FAILED:motor fault')
    def test_raw_done_does_not_release_ui_ownership_while_receiver_is_running(self):
        job=self.job();waiting=threading.Event();release=threading.Event()
        def wait():waiting.set();release.wait(1);return 0
        process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO('A_DONE\n'),wait=wait,poll=Mock(return_value=None))
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen',return_value=process):
            worker=threading.Thread(target=job.run);worker.start()
            try:
                self.assertTrue(waiting.wait(1));job.poll();self.assertTrue(job.busy)
                self.assertIs(self.app.remote.execution_owner,job)
            finally:release.set();worker.join(1)
            job.poll()

    def test_malformed_results_never_authorize_success(self):
        for value in (False,'0',-1,{},None):
            with self.subTest(value=value):
                job=self.job();message='GUI_RESULT:'+json.dumps({'execution_id':job.request['execution_id'],'exit_code':value})
                self.assertEqual(self.run_stream(job,['A_DONE',message],0),'failed')

    def test_duplicate_result_is_finalized_only_once(self):
        job=self.job();result=self.result(job,0)
        self.assertEqual(self.run_stream(job,['A_DONE',result,result],0),'done')
        self.assertEqual(sum(e['status']=='GUI_RESULT:0' for e in job.record['events']),1)
        self.app.remote.rpc.assert_not_called()

    def test_user_cancel_during_stream_remains_stopped_after_valid_result(self):
        job=self.job();started=threading.Event();release=threading.Event()
        def output():
            yield 'A_EPISODE_STARTED\n'
            started.set();release.wait(1)
            yield self.result(job,0)+'\n'
        process=SimpleNamespace(stdin=io.StringIO(),stdout=output(),wait=Mock(return_value=0),poll=Mock(return_value=None))
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen',return_value=process):
            worker=threading.Thread(target=job.run);worker.start()
            try:
                self.assertTrue(started.wait(1));job.cancel();job.cancel_worker.join(1)
            finally:release.set();worker.join(1)
            job.poll()
        self.assertEqual(job.record['state'],'stopped')
        self.app.remote.rpc.assert_called_once_with('command',{'action':'hold'})

    def test_failure_hold_finishes_before_ownership_release_and_never_repeats(self):
        job=self.job();holding=threading.Event();release=threading.Event()
        def hold(*args):holding.set();release.wait(1)
        self.app.remote.rpc.side_effect=hold
        def output():
            yield self.result(job,1)+'\n'
            yield self.result(job,1)+'\n'
            raise OSError('late stream failure')
        process=SimpleNamespace(stdin=io.StringIO(),stdout=output(),wait=Mock(return_value=1),poll=Mock(return_value=1))
        with patch.object(gui,'command_args',return_value=['fake-ssh']),patch.object(gui.subprocess,'Popen',return_value=process):
            worker=threading.Thread(target=job.run);worker.start()
            try:
                self.assertTrue(holding.wait(1));job.poll();self.assertTrue(job.busy)
                self.assertIs(self.app.remote.execution_owner,job)
            finally:release.set();worker.join(1)
            job.poll()
        self.assertEqual(job.record['state'],'failed')
        self.app.remote.rpc.assert_called_once_with('command',{'action':'hold'})


class ReceiverResultTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
    def run_receiver(self,request,execute,*,check=False):
        stream=io.StringIO();root=Path(self.temp.name)
        argv=['receiver',str(root)]+(['--check'] if check else [])
        job=(root,{'steps':[{}]}, {},100.,20.)
        with patch.object(sys,'argv',argv),patch.object(sys,'stdin',io.StringIO(json.dumps(request))),patch.object(sys,'stdout',stream),patch.object(sys,'path',list(sys.path)),patch.dict(sys.modules,{'episode_cli':cli}),patch.object(cli,'ROOT',root),patch('subprocess.Popen',side_effect=AssertionError('test must not start a process')),patch.object(receiver,'validate_snapshot',return_value=job),patch.object(receiver,'install_adapters') as install,patch.object(cli,'main',side_effect=execute) as main:
            self.last_install=install;self.last_main=main
            try:code=receiver.main()
            except Exception:
                self.captured=stream.getvalue();raise
        return code,stream.getvalue(),install,main
    def test_result_follows_cleanup_for_success_reset_and_failure(self):
        request={'arm':'arm2','episode':{'product_type':'A'},'execution_id':'a'*32}
        for code in (0,130,1):
            with self.subTest(code=code):
                def execute(_):print('cleanup completed');return code
                result,output,_,_=self.run_receiver(request,execute)
                self.assertEqual(result,code)
                self.assertEqual(gui.execution_result(output.splitlines()[-1],request['execution_id']),code)
                self.assertEqual(output.splitlines()[0],'cleanup completed')
    def test_cleanup_failure_after_human_done_emits_no_verified_result(self):
        def execute(_):print('A_DONE');raise RuntimeError('publisher cleanup failed')
        with self.assertRaisesRegex(RuntimeError,'cleanup failed'):
            self.run_receiver({'arm':'arm2','episode':{},'execution_id':'a'*32},execute)
        self.assertNotIn('GUI_RESULT:',self.captured)
    def test_invalid_execution_id_is_rejected_before_adapter_installation(self):
        request={'arm':'arm2','episode':{},'execution_id':'invalid'}
        with self.assertRaisesRegex(ValueError,'식별자'):self.run_receiver(request,Mock())
        self.last_install.assert_not_called();self.last_main.assert_not_called()
    def test_file_check_does_not_require_execution_id_or_start_cli(self):
        code,output,install,main=self.run_receiver({'arm':'arm2','episode':{}},Mock(),check=True)
        self.assertEqual(code,0);self.assertIn('CHECK_OK',output)
        install.assert_not_called();main.assert_not_called()
