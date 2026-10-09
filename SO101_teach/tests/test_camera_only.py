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
    def test_startup_waits_for_pi_read_before_camera_connection(self):
        from concurrent.futures import Future
        a=self.app;a.camera_only=True;a.episode_sync.job=Future()
        with patch.object(a.settings.pi_panel,'connect') as connect:
            a.startup_camera_only();connect.assert_not_called();a.episode_sync.job=None;a.startup_camera_only();connect.assert_called_once()
        self.assertEqual(a.page,'camera');self.assertFalse(a.startup_devices_pending)

    def test_camera_only_does_not_start_linear_monitor_or_switch_to_local_usb(self):
        a=self.app;a.camera_only=True
        with patch('so101_teach.linear_state.StateStream') as stream:
            a.start_linear_state_query();stream.assert_not_called()
        with self.assertRaisesRegex(ValueError,'카메라 전용'):a.settings.pi_panel.use_local()

class CameraOnlyWorkspaceTests(unittest.TestCase):
    from tests import test_arm_workspaces as fixtures
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    def test_switch_to_unconnected_arm_connects_only_its_pi_camera(self):
        self.manager.camera_only=True
        for app in self.manager.apps.values():app.camera_only=True
        self.a.show_page('camera');self.b.camera_view_requested=True
        with patch.object(self.b.settings.pi_panel,'connect') as connect,patch.object(self.b,'start_camera') as local:
            self.manager.select('arm3');connect.assert_called_once();local.assert_not_called()
        self.assertTrue(self.b.remote_mode);self.assertIsNone(self.b.session);self.assertIsNone(self.b.leader_session)
    def test_hidden_arm_does_not_reclaim_camera_when_startup_retry_finishes(self):
        self.b.camera_only=True
        with patch.object(self.b.settings.pi_panel,'connect') as connect:
            self.b.startup_camera_only();connect.assert_not_called()

@unittest.skipUnless(os.environ.get('SO101_LIVE_CAMERA')=='1','Explicit camera-only hardware test')
class LiveCameraOnlyTests(unittest.TestCase):
    def test_real_pi_camera_and_two_episode_libraries_without_motor_requests(self):
        import tkinter as tk
        import cv2
        from PIL import ImageGrab
        from so101_teach.arm_workspaces import ArmWorkspaces
        from so101_teach.remote_client import RemoteLink
        out=ROOT/'verification/current-integration-audit';out.mkdir(exist_ok=True)
        calls=[];original=RemoteLink.rpc
        def guarded(link,method,args=None,timeout=3.):
            calls.append(method)
            if method not in ('configure','detect_clear','detect_freeze'):raise AssertionError('Forbidden non-camera RPC: '+method)
            return original(link,method,args,timeout)
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            for name in ('profile.json','robot_profiles.json','jigs.json','workcell-preview.json','pi-connection.json'):shutil.copy2(ROOT/'data'/name,folder/name)
            shutil.copytree(ROOT/'data/calibration',folder/'calibration')
            root=tk.Tk();root.withdraw();manager=None
            try:
                with patch.object(RemoteLink,'rpc',guarded):
                    manager=ArmWorkspaces(root,folder,render=False,auto_camera=True,auto_devices=False,camera_only=True)
                    a=manager.apps[manager.active];root.geometry('1480x920');root.deiconify();deadline=time.monotonic()+35
                    while time.monotonic()<deadline:
                        root.update();time.sleep(.02)
                        if a.camera and a.camera.running and a.camera_view_observation():break
                    self.assertIsNotNone(a.camera,a.settings.pi_panel.status.get());self.assertTrue(a.camera.running,a.camera.error)
                    frames=set();ages=[];start=time.monotonic();end=start+8
                    while time.monotonic()<end:
                        root.update();time.sleep(.02);view=a.camera_view_observation()
                        if view:frames.add(view[2]);ages.append(time.monotonic()-view[2])
                    self.assertGreater(len(frames),15);self.assertTrue(all(x.session is None and x.leader_session is None for x in manager.apps.values()))
                    self.assertTrue(all(x.episode_sync.ready for x in manager.apps.values()))
                    frame,_,_=a.camera_view_observation();cv2.imwrite(str(out/'live-camera.jpg'),frame)
                    root.update();ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())).save(out/'live-camera-app.png')
                    capture_seconds=time.monotonic()-start
                    self.assertIsNone(manager.linear_state_query)
                    switched={}
                    for key in (next(k for k in manager.apps if k!=manager.active),'arm2'):
                        manager.select(key);current=manager.apps[key];deadline=time.monotonic()+25
                        while time.monotonic()<deadline:
                            root.update();time.sleep(.02)
                            if current.camera and current.camera.running and current.camera_view_observation():break
                        self.assertIsNotNone(current.camera,current.message.get());self.assertTrue(current.camera.running,current.camera.error)
                        observation=current.camera_view_observation();self.assertIsNotNone(observation,current.camera.error)
                        switched[key]={'preview_age_seconds':time.monotonic()-observation[2],'motor_session':current.session is not None}
                    self.assertTrue(all(x.session is None and x.leader_session is None for x in manager.apps.values()))
                    report={'camera_handoffs':switched,'frames':len(frames),'seconds':capture_seconds,'max_preview_age_seconds':max(ages),'camera_error':a.camera.error,'rpc_methods':calls,'motor_sessions':0,'episodes':{k:len(x.episode_sync.snapshot['episodes']) for k,x in manager.apps.items()}}
                    (out/'live-camera-report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
                    manager.close()
                    deadline=time.monotonic()+8
                    while len(manager.finished)<len(manager.apps) and time.monotonic()<deadline:root.update();time.sleep(.03)
            finally:
                if manager and not manager.closing:manager.close()
