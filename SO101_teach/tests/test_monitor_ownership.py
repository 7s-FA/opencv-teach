import time,threading
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests.test_work_monitor_ui import MonitorUITests,FakeObserver
from desktop_monitor.ownership import prepare


class MonitorOwnershipTests(MonitorUITests):
    def link(self):
        return SimpleNamespace(close=Mock(),listeners={},state_thread=None,leader_thread=None)

    def test_observation_releases_both_idle_teaching_leases_without_motor_disconnect(self):
        app=self.app;first=self.link();second=self.link();app.remote=first
        other=SimpleNamespace(**app.__dict__);other.remote=second
        # Separate recovery flags, as on real per-arm windows.
        other.settings=SimpleNamespace(**app.settings.__dict__)
        other.settings.pi_panel=SimpleNamespace(job=None,auto_enabled=True,recovery_pending=True,restore_devices=True)
        manager=SimpleNamespace(apps={'arm2':app,'arm3':other},pi_execution=None)
        app.workspace_manager=other.workspace_manager=manager
        try:
            p=app.camera_work_monitor;p.factory=FakeObserver
            app.camera_tabs.select(p);app.show_page('camera')
            until=time.monotonic()+3
            while p.observer is None and time.monotonic()<until:self.root.update();time.sleep(.01)
            self.assertIsNotNone(p.observer)
            for window,link in ((app,first),(other,second)):
                link.close.assert_called_once_with(disconnect=False)
                self.assertTrue(window.work_observation_mode);self.assertIsNone(window.remote)
                self.assertFalse(window.settings.pi_panel.auto_enabled)
            with patch.object(app.settings.pi_panel,'schedule_recovery') as recover:
                app.settings.pi_panel.poll();recover.assert_not_called()
        finally:app.workspace_manager=None

    def test_active_local_program_keeps_its_lease(self):
        app=self.app;link=self.link();app.remote=link
        running=threading.Event();running.set()
        app.session=SimpleNamespace(running=True,state='MOVING',program_active=running,command_pending=threading.Event())
        try:
            self.assertTrue(prepare(app));link.close.assert_not_called()
            self.assertIs(app.remote,link);self.assertFalse(getattr(app,'work_observation_mode',False))
        finally:app.remote=None;app.session=None

    def test_observation_suppresses_auto_connect_and_requires_leaving_tab_for_manual(self):
        self.prepare();app=self.app
        with patch('so101_teach.remote_client.RemoteLink.open',side_effect=AssertionError('No acquire')):
            app.settings.pi_panel.connect(automatic=True)
            with self.assertRaisesRegex(ValueError,'조회 전용'):app.settings.pi_panel.connect()

    def test_runtime_accepts_external_owner_after_monitor_handoff_without_opening_motors(self):
        from so101_teach.remote_server import Runtime
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            motion=Mock(side_effect=AssertionError('No hardware'))
            runtime=Runtime(folder,motion_factory=motion)
            try:
                lease=runtime.acquire()['lease']
                with self.assertRaisesRegex(ValueError,'다른 PC 창'):runtime.acquire()
                link=self.link();link.close.side_effect=lambda **kwargs:runtime.detach(lease)
                self.app.remote=link;p=self.prepare()
                until=time.monotonic()+2
                while p.observer is None and time.monotonic()<until:self.root.update();time.sleep(.01)
                self.assertIsNotNone(p.observer);self.assertTrue(runtime.detached)
                self.assertNotEqual(runtime.acquire()['lease'],lease)
                motion.assert_not_called()
            finally:runtime.close()

    def test_busy_work_is_released_only_after_becoming_idle(self):
        app=self.app;link=self.link();app.remote=link
        running=threading.Event();running.set()
        app.session=SimpleNamespace(running=True,state='MOVING',program_active=running,command_pending=threading.Event())
        try:
            p=self.prepare();self.assertIsNotNone(p.observer);link.close.assert_not_called()
            app.session.state='READ_ONLY';running.clear()
            until=time.monotonic()+2
            while not link.close.called and time.monotonic()<until:self.root.update();time.sleep(.01)
            link.close.assert_called_once_with(disconnect=False)
        finally:app.remote=None;app.session=None
