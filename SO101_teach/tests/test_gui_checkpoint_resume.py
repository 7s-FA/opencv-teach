"""GUI retry uses the same job/stop fence, never another episode launch."""
import io,json,os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from tests.test_gui_result_protocol import ResultProtocolTests as ProtocolFixture
from tests.test_gui_pi_execution import GuiExecutionTests as GuiFixture
from so101_teach import pi_execution as gui


class GuiRetryTransportTests(TestCase):
    setUp=ProtocolFixture.setUp
    job=ProtocolFixture.job
    def paused_job(self):
        job=self.job();job.process=SimpleNamespace(stdin=io.StringIO(),poll=Mock(return_value=None))
        job.control_progress('A_RECOVERY_CONTEXT:'+json.dumps({'job_id':job.request['execution_id'],'stop_generation':2,'recheck_required':True}))
        job.control_progress('A_PAUSED');return job
    def test_resume_sends_one_fenced_request_and_remains_paused_until_pass(self):
        job=self.paused_job();job.restart();job.restart_worker.join(1)
        line=job.process.stdin.getvalue();self.assertTrue(line.startswith('control '))
        request=json.loads(line[len('control '):]);self.assertEqual(request['job_id'],job.request['execution_id'])
        self.assertEqual(request['stop_generation'],2);self.assertEqual(request['command'],'restart')
        self.assertFalse(job.can_restart);self.assertTrue(job.paused)
        with self.assertRaises(ValueError):job.restart()
        job.control_progress('A_RECOVERY_RECHECK_STARTED');self.assertTrue(job.rechecking)
        job.control_progress('A_RESUMED');self.assertFalse(job.paused);self.assertFalse(job.restart_pending)
    def test_rejected_recheck_enables_an_explicit_new_attempt_without_relaunch(self):
        job=self.paused_job();job.restart();job.restart_worker.join(1)
        job.control_progress('A_RECOVERY_RECHECK_STARTED');job.control_progress('A_RECOVERY_BLOCKED:still missing')
        self.assertTrue(job.can_restart);self.assertTrue(job.paused)
    def test_stale_context_and_changed_connection_cannot_authorize_retry(self):
        job=self.paused_job()
        for job_id,generation in ((job.request['execution_id'],1),('b'*32,4)):
            job.control_progress('A_RECOVERY_CONTEXT:'+json.dumps({'job_id':job_id,'stop_generation':generation}))
        self.assertEqual(job.pause_generation,2)
        job.app.remote=SimpleNamespace()
        with self.assertRaises(RuntimeError):job.restart()
        self.assertEqual(job.process.stdin.getvalue(),'')
    def test_cancel_before_sender_runs_suppresses_queued_restart(self):
        job=self.paused_job()
        with patch.object(gui.threading,'Thread') as thread:
            job.restart();send=thread.call_args.kwargs['target']
        job.cancelled.set();send();self.assertEqual(job.process.stdin.getvalue(),'')


class GuiRetryButtonTests(TestCase):
    setUp=GuiFixture.setUp
    tearDown=GuiFixture.tearDown
    ready=GuiFixture.ready
    def test_existing_execution_button_shows_retry_and_never_starts_another_job(self):
        self.ready();job=SimpleNamespace(app=self.a,busy=True,paused=True,rechecking=False,
            restart_pending=False,recheck_required=True,can_restart=True,restart=Mock())
        self.manager.pi_execution=job
        try:
            self.a.root.geometry('1920x1080+0+0');self.b.root.withdraw();self.a.show_page('teach')
            permissions=self.a.motion_button_permissions(True,True,'HOLD');self.a.apply_command_permissions(permissions)
            self.root.update()
            self.assertEqual(self.a.execute_btn.cget('text'),'재검사 후 이어서 실행')
            self.assertNotIn('disabled',self.a.execute_btn.state())
            self.a.execute_episode();job.restart.assert_called_once()
            import tkinter.font as tkfont
            font=tkfont.nametofont('TkDefaultFont')
            self.assertLess(font.measure(self.a.execute_btn.cget('text')),self.a.execute_btn.winfo_width()-12)
            from PIL import ImageGrab
            target=Path(__file__).resolve().parents[1]/'verification/checkpoint-resume-20261027/gui-paused.png'
            ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(target)
            job.rechecking=True;job.can_restart=False
            self.a.apply_command_permissions(permissions)
            self.assertIn('disabled',self.a.execute_btn.state());self.assertEqual(self.a.execute_btn.cget('text'),'재검사 중…')
            self.b.apply_command_permissions(self.b.motion_button_permissions(True,True,'HOLD'))
            self.assertIn('disabled',self.b.execute_btn.state())
        finally:self.manager.pi_execution=None
