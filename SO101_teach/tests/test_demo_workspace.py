import hashlib,json,shutil,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from so101_teach.domain import ROOT
from so101_teach.demo_workspace import prepare_data,activate,ReplayCamera
from so101_teach.arm_workspaces import ArmWorkspaces

class DemoWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=prepare_data(destination=Path(self.temp.name)/'demo')
        self.source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for name in ('profile.json','robot_profiles.json','preferences.json','workcell-preview.json') if (p:=ROOT/'data'/name).exists()}
        self.root=tk.Tk();self.root.withdraw()
        self.blocks=[patch('cv2.VideoCapture',side_effect=AssertionError('No real camera')),patch('so101_teach.motion.new_bus',side_effect=AssertionError('No serial bus')),patch('so101_teach.remote_client.RemoteLink.open',side_effect=AssertionError('No Pi connection')),patch('so101_teach.episode_transfer.remote_request',side_effect=AssertionError('No Pi transfer'))]
        for block in self.blocks:block.start()
        self.manager=ArmWorkspaces(self.root,self.data,render=False,auto_camera=True,auto_devices=False,mode_override='demo');activate(self.manager)
        self.app=self.manager.apps[self.manager.active];self.root.geometry('1920x1080');self.root.deiconify();self.root.update()
    def tearDown(self):
        try:
            self.manager.finish_safe_close();deadline=time.monotonic()+5
            while any(not a.closed or (a.camera and a.camera.running) or (a.session and a.session.running) for a in self.manager.apps.values()) and time.monotonic()<deadline:
                try:self.root.update()
                except tk.TclError:break
                time.sleep(.02)
            for path,digest in self.source_hashes.items():self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),digest)
        finally:
            for block in reversed(self.blocks):block.stop()
            self.temp.cleanup()
    def wait_for(self,condition,seconds=8):
        deadline=time.monotonic()+seconds
        while not condition() and time.monotonic()<deadline:self.root.update();time.sleep(.03)
        self.assertTrue(condition(),self.app.message.get())
    def test_latest_two_arm_shell_uses_only_demo_sessions_and_isolated_data(self):
        self.assertEqual(set(self.manager.apps),{'arm2','arm3'})
        for a in self.manager.apps.values():
            self.assertEqual(set(a.arm_quick_buttons),{'arm2','arm3'});self.assertTrue(a.demo_mode);self.assertFalse(a.remote_mode)
            self.assertEqual(a.session.__class__.__name__,'DemoSession');self.assertEqual(a.data_dir,self.data)
        self.wait_for(lambda:all(a.session.latest for a in self.manager.apps.values()))
        self.manager.select('arm3');self.assertEqual(self.manager.active,'arm3')
        self.manager.apps['arm3'].notice('独立デモ')
        (self.data/'preferences.json').write_text('{}')
    def test_recorded_camera_runs_real_inspector_and_C_labels_and_loss_recovers(self):
        a=self.app;a.show_page('camera');a.camera_tabs.select(a.camera_inspection)
        self.wait_for(lambda:a.camera_inspection.capture_snapshot is not None and len(a.camera_inspection.label_rectangles)==9)
        self.assertIsInstance(a.camera,ReplayCamera);self.assertIsNone(a.camera.error)
        self.assertTrue(a.camera_inspection.capture_snapshot['metadata']['rows'])
        a.demo_video_gap=True;self.wait_for(lambda:a.camera_inspection.capture_snapshot is None,3)
        a.demo_video_gap=False;self.wait_for(lambda:a.camera_inspection.capture_snapshot is not None)
        a.root.event_generate('<F6>');self.root.update();self.assertEqual(a.demo_scene,'faults')
    def test_all_explicit_hardware_entrypoints_reject_demo(self):
        a=self.app
        calls=[a.require_remote,a.settings.pi_panel.probe,a.settings.pi_panel.connect,a.episode_sync.refresh,a.settings.start_calibration,a.prepare_leader_assist,lambda:a.start_leader_connection({},a.calibration),lambda:a.settings.episode_transfer_panel.start_job('query',{},{} )]
        for call in calls:
            with self.assertRaisesRegex(ValueError,'데모'):call()
    def test_replay_source_keeps_capture_and_detection_timestamps_distinct(self):
        a=self.app;a.show_page('camera');self.wait_for(lambda:a.camera and a.camera.observation)
        obs=a.camera.preview_observation
        self.assertLessEqual(obs[1]['detection_frame_at'],obs[2]);self.assertGreaterEqual(obs[1]['preview_detection_age_s'],0)
        self.assertEqual(tuple(a.camera.frame.shape[:2]),(720,1280))
