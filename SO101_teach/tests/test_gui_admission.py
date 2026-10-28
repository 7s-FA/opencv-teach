"""GUI preparation faults use fake RPCs and a non-Tk scheduler; no hardware."""
import tempfile,threading,time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock,patch
from so101_teach import pi_execution as gui

class GuiAdmissionTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.manager=SimpleNamespace(apps={},pi_execution=None)
        for key in ('arm2','arm3'):
            app=SimpleNamespace(workspace_manager=self.manager,data_dir=Path(self.temp.name),
                remote=SimpleNamespace(config={},rpc=Mock(return_value={}),error=None,stop=threading.Event()),
                camera=None,suspend_camera=Mock(),notice=Mock(),set_mode=Mock(),root=SimpleNamespace(after=Mock()))
            self.manager.apps[key]=app
        self.app=self.manager.apps['arm2']
        p=patch.object(gui,'execution_request',return_value={'arm':'arm2','episode':{'id':'a'*32,'name':'selected'}})
        p.start();self.addCleanup(p.stop)

    def test_claim_runs_off_ui_and_failure_releases_both_owners(self):
        owner=threading.get_ident();seen=[]
        def fail(*args):seen.append(threading.get_ident());raise OSError('claim unavailable')
        self.app.remote.rpc.side_effect=fail
        with patch.object(gui.subprocess,'Popen') as process:
            job=gui.PiExecution(self.app)
            job.prepare_thread.join(1);job.poll()
        self.assertNotEqual(seen,[owner]);self.assertFalse(job.busy)
        self.assertTrue(all(a.remote.execution_owner is None for a in self.manager.apps.values()))
        process.assert_not_called()

    def test_cancel_during_claim_cannot_start_ssh_or_release_ownership_early(self):
        entered=threading.Event();release=threading.Event()
        def claim(method,*args):
            if method=='claim_episode':entered.set();release.wait(1)
        self.app.remote.rpc.side_effect=claim
        with patch.object(gui.subprocess,'Popen') as process:
            job=gui.PiExecution(self.app)
            try:
                self.assertTrue(entered.wait(1));job.cancel();job.poll()
                self.assertTrue(job.busy)
            finally:release.set()
            job.prepare_thread.join(1);job.cancel_worker.join(1);job.poll()
        self.assertFalse(job.busy);process.assert_not_called()

    def test_record_failure_is_reported_without_claiming_or_launching(self):
        with patch.object(gui,'atomic_json',side_effect=OSError('disk full')),patch.object(gui.subprocess,'Popen') as process:
            job=gui.PiExecution(self.app)
            job.prepare_thread.join(1);job.poll()
        self.assertFalse(job.busy)
        for app in self.manager.apps.values():app.remote.rpc.assert_not_called()
        process.assert_not_called()

    def test_gui_preparation_preserves_idle_release_on_the_other_arm(self):
        for selected in ('arm2','arm3'):
            with self.subTest(selected=selected):
                job=object.__new__(gui.PiExecution);job.app=self.manager.apps[selected]
                job.apps=list(self.manager.apps.values());job.cancelled=threading.Event();job.prepared=threading.Event()
                job.links=[(key,app,app.remote,None) for key,app in self.manager.apps.items()]
                job.link=job.app.remote;job.config={};job.request={}
                job.path=Path(self.temp.name)/'record.json';job.record={}
                for app in job.apps:app.remote.rpc.reset_mock()
                job.prepare()
                for app in job.apps:
                    if app is job.app:app.remote.rpc.assert_called_once_with('claim_episode')
                    else:app.remote.rpc.assert_called_once_with('claim_episode',{'preserve_idle_release':True})
