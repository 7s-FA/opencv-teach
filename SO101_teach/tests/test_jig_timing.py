import time,unittest,tkinter as tk
from unittest.mock import patch
from types import SimpleNamespace
from copy import deepcopy
from pathlib import Path
from PIL import ImageGrab
from tests import test_ui as fixtures
from so101_teach.domain import read_json,atomic_json
from so101_teach.ui import App
from so101_teach.remote_config import configuration_bundle

class TimingUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def test_settings_persist_and_apply_to_existing_new_and_remote_jigs(self):
        a=self.app;a.acquisition_seconds.set('10');a.measurement_attempts.set('5');a.hold_seconds.set('7');a.save_camera_hold()
        other=a.catalog.duplicate('pallet');a.detector.refresh()
        self.assertEqual(a.detector.latches[other['id']].stability.seconds,10)
        self.assertEqual(configuration_bundle(a)['acquisition_seconds'],10)
        a.save_model();self.assertEqual(a.pose_latch.stability.seconds,10)
        a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.pose_latch.stability.seconds,10);self.assertEqual(self.app.measurement_attempts_limit,5)
        self.assertEqual(self.app.measurement_timeout_seconds,50);self.assertEqual(self.app.pose_latch.seconds,7)
    def test_legacy_interval_and_timeout_are_not_reinterpreted_as_new_units(self):
        a=self.app;a.close();atomic_json(self.data/'preferences.json',{'jig_accept_seconds':.7,'jig_measurement_timeout_seconds':10})
        before=(self.data/'preferences.json').read_bytes();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.pose_latch.stability.seconds,3);self.assertEqual(self.app.measurement_attempts_limit,3)
        # Opening the UI does not migrate unrelated saved records.
        self.assertEqual((self.data/'preferences.json').read_bytes(),before)
    def test_invalid_times_or_fractional_attempts_do_not_modify_saved_settings(self):
        a=self.app;a.save_camera_hold();before=read_json(self.data/'preferences.json')
        for seconds,attempts in (('nan','3'),('2.9','1'),('10.1','5'),('3','0'),('3','6'),('3','1.5')):
            a.acquisition_seconds.set(seconds);a.measurement_attempts.set(attempts)
            with self.assertRaises(ValueError):a.save_camera_hold()
            self.assertEqual(read_json(self.data/'preferences.json'),before)
    def test_one_and_five_attempt_limits_fail_without_dispatch_or_old_pose_fallback(self):
        a=self.app
        step=a.store.step(a.target,'test');step.update(jig_id='pallet',jig_reference={'pose':[100,100,0],'symmetry_deg':90,'stl_sha256':a.catalog.mesh('pallet')['sha256']})
        for attempts in (1,5):
            a.measurement_attempts_limit=attempts;a.pose_latch.stability.configure(3)
            a.pending_execution={'started':time.monotonic()-3*attempts-.01,'page':'teach','action':'play','steps':[step]}
            with patch.object(a,'begin_execution') as begin,patch.object(a,'suspend_camera'):
                a.check_pending_execution();self.assertIsNone(a.pending_execution);begin.assert_not_called()
            self.assertIn(f'{attempts}회',a.message.get());self.assertIn('최소 2회',a.message.get())
        # A delayed successful result from an extra attempt cannot bypass the limit.
        a.measurement_attempts_limit=1;self.fake_jig_camera();a.camera.observation[1]['acquisition_completed_attempts']=2
        a.pending_execution={'started':time.monotonic()-.1,'page':'teach','action':'play','steps':[step]}
        with patch.object(a,'begin_execution') as begin,patch.object(a,'suspend_camera'):
            a.check_pending_execution();self.assertIsNone(a.pending_execution);begin.assert_not_called()
    def test_completed_attempts_fail_before_next_window_and_two_observations_are_not_enough_if_inconsistent(self):
        a=self.app;self.fake_jig_camera();frame,_,at=a.camera.observation
        for count,reason in ((1,'관측 부족'),(3,'위치 불일치')):
            a.camera.observation=(frame,{'selected':None,'status':'acquisition_failed','stable_candidate_samples':count,'acquisition_issue':reason,'acquisition_completed_attempts':1},at)
            a.measurement_attempts_limit=1
            self.assertIn(reason,a.measurement_failure({'started':time.monotonic()},time.monotonic(),['pallet']))
            a.measurement_attempts_limit=2;self.assertIsNone(a.measurement_failure({'started':time.monotonic()},time.monotonic(),['pallet']))
    def test_controls_and_camera_render_fit_both_sizes(self):
        a=self.app;a.show_page('camera')
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update()
            for w,low,high in ((a.acquisition_seconds_spin,3,10),(a.measurement_attempts_spin,1,5),(a.hold_seconds_spin,1,10)):
                self.assertEqual(float(w.cget('from')),low);self.assertEqual(float(w.cget('to')),high)
                self.assertGreater(w.winfo_width(),30);self.assertLessEqual(w.winfo_rootx()+w.winfo_width(),self.root.winfo_rootx()+width)
                self.assertLessEqual(w.winfo_x()+w.winfo_width(),w.master.winfo_width())
            self.assertGreater(a.camera_canvas.winfo_height(),200)
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/acquisition-controls-{width}x{height}.png')
