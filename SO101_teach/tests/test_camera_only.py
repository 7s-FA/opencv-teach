import json,os,shutil,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_ui
from so101_teach.domain import ROOT

class CameraOnlyTests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown
    def test_motor_connection_motion_leader_and_calibration_are_blocked(self):
        a=self.app;a.camera_only=True
        for call in (a.connect_devices,lambda:a.motion_request('arm'),lambda:a.motion_request('move',[a.target]),lambda:a.start_leader_connection({},a.calibration),a.settings.start_calibration):
            with self.assertRaisesRegex(ValueError,'카메라 전용'):call()
        a.startup_connections();a.connect_startup_devices();self.assertIsNone(a.session)
    def test_startup_opens_work_observer_without_control_connection(self):
        a=self.app;a.camera_only=True
        with patch.object(a.settings.pi_panel,'connect') as connect,patch.object(a.camera_work_monitor,'activate'):
            a.startup_camera_only();connect.assert_not_called()
        self.assertEqual(a.page,'camera');self.assertEqual(a.camera_tabs.select(),str(a.camera_work_monitor))
        self.assertFalse(a.startup_devices_pending)

    def test_camera_only_does_not_start_linear_monitor_or_switch_to_local_usb(self):
        a=self.app;a.camera_only=True
        with patch('so101_teach.linear_state.StateStream') as stream:
            a.start_linear_state_query();stream.assert_not_called()
        with self.assertRaisesRegex(ValueError,'카메라 전용'):a.settings.pi_panel.use_local()

class CameraOnlyWorkspaceTests(unittest.TestCase):
    from tests import test_arm_workspaces as fixtures
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    def test_switch_to_passive_arm_starts_observer_without_control(self):
        self.manager.camera_only=True
        for app in self.manager.apps.values():app.camera_only=True
        self.a.show_page('camera');self.b.camera_view_requested=True;self.b._work_monitor_passive=True
        with patch.object(self.b.settings.pi_panel,'connect') as connect,patch.object(self.b,'start_camera') as local,patch.object(self.b.camera_work_monitor,'activate'):
            self.manager.select('arm3');connect.assert_not_called();local.assert_not_called()
        self.assertTrue(self.b.remote_mode);self.assertIsNone(self.b.session);self.assertIsNone(self.b.leader_session)
    def test_hidden_arm_does_not_reclaim_camera_when_startup_retry_finishes(self):
        self.b.camera_only=True
        with patch.object(self.b.settings.pi_panel,'connect') as connect:
            self.b.startup_camera_only();connect.assert_not_called()

@unittest.skipUnless(os.environ.get('SO101_LIVE_CAMERA')=='1','Explicit camera-only hardware test')
class LiveCameraOnlyTests(unittest.TestCase):
    def test_real_pi_camera_and_two_episode_libraries_without_motor_requests(self):
        import tkinter as tk
        from PIL import ImageGrab
        import numpy as np
        from so101_teach.arm_workspaces import ArmWorkspaces
        from so101_teach.remote_client import RemoteLink
        out=ROOT/'verification/manual-control-20261026';out.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            for name in ('profile.json','robot_profiles.json','jigs.json','workcell-preview.json','pi-connection.json'):shutil.copy2(ROOT/'data'/name,folder/name)
            shutil.copytree(ROOT/'data/calibration',folder/'calibration')
            root=tk.Tk();root.withdraw();manager=None
            def wait_for(predicate):
                deadline=time.monotonic()+35
                while time.monotonic()<deadline:
                    root.update();time.sleep(.02)
                    if predicate():return
                self.fail('Camera observer or Pi episode library not ready: '+str({k:{'page':a.page,'tab':a.camera_tabs.tab(a.camera_tabs.select(),'text'),'status':a.camera_work_monitor.connection.get()} for k,a in manager.apps.items()}))
            try:
                with patch.object(RemoteLink,'open',side_effect=AssertionError('No control lease')),patch.object(RemoteLink,'rpc',side_effect=AssertionError('No control RPC')):
                    manager=ArmWorkspaces(root,folder,render=False,auto_camera=True,auto_devices=False,camera_only=True)
                    root.geometry('1920x1080+0+0');root.deiconify()
                    wait_for(lambda:all(a.episode_sync.ready for a in manager.apps.values()))
                    observed={}
                    for key in ('arm2','arm3','arm2'):
                        manager.select(key);a=manager.apps[key]
                        panel=a.camera_work_monitor
                        wait_for(lambda:panel.connected and panel.frame is not None)
                        self.assertGreater(np.asarray(panel.frame).std(),2)
                        observed[key]={'camera_shape':list(np.asarray(panel.frame).shape),'observer_connected':panel.connected}
                        self.assertTrue(all(x.remote is None and x.session is None and x.leader_session is None for x in manager.apps.values()))
                    ImageGrab.grab().save(out/'camera-only-observer.png')
                    self.assertIsNone(manager.linear_state_query)
                    (out/'camera-only-observer.json').write_text(json.dumps({'arms':observed,'control_acquisitions':0,'motion_commands':0},indent=2))
            finally:
                if manager:
                    manager.finish_safe_close()
                    deadline=time.monotonic()+8
                    while len(manager.finished)<len(manager.apps) and time.monotonic()<deadline:
                        try:root.update()
                        except tk.TclError:break
                        time.sleep(.03)
