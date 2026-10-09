import threading
from collections import OrderedDict
import time,unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from so101_teach.camera_lifecycle import CameraLifecycle
from so101_teach.remote_server import Runtime
from tests.test_pose_hold import result

class AdoptionLatencyTests(unittest.TestCase):
    def app(self,age=3.2):
        r=result(strong=True);r.update(pose_measured_at=99.,pose_held=True,hold_remaining_s=9.)
        camera=SimpleNamespace(observation=(np.zeros((20,30,3),np.uint8),{'by_jig':{'pallet':r}},100.-age),running=True,error=None)
        a=SimpleNamespace(camera=camera,camera_sleeping=False,active_jig='pallet',pose_latch=SimpleNamespace(seconds=10.),measurement_valid_after=0.,pending_execution=None,camera_task=None,teaching_jig_hold_active=lambda:False)
        a.camera_results=lambda now=None:CameraLifecycle.camera_results(a,now)
        return a
    def test_valid_adoption_remains_visible_without_loosening_new_measurement_gate(self):
        a=self.app();before=deepcopy(a.camera.observation[1])
        self.assertEqual(a.camera_results(100.),{})
        display=CameraLifecycle.camera_adoption_results(a,100.)
        self.assertTrue(display['pallet']['selected']);self.assertTrue(display['pallet']['display_only'])
        self.assertEqual(display['pallet']['hold_remaining_s'],9.)
        self.assertEqual(a.camera.observation[2],96.8);self.assertEqual(a.camera.observation[1],before)
        a.pending_execution={'started':100.};self.assertEqual(a.camera_results(100.),{})
    def test_display_does_not_revive_expired_invalidated_or_failed_camera(self):
        for mode in ('expired','invalidated','failure','recovering','future'):
            a=self.app()
            if mode=='expired':a.camera.observation[1]['by_jig']['pallet']['pose_measured_at']=89.
            elif mode=='invalidated':a.measurement_valid_after=99.5
            elif mode=='failure':a.camera.error='Camera disconnected'
            elif mode=='recovering':a.camera.recovering=True
            else:a.camera.observation[1]['by_jig']['pallet']['pose_measured_at']=101.
            self.assertEqual(CameraLifecycle.camera_adoption_results(a,100.),{},mode)
    def test_confirmation_progress_never_becomes_a_selected_pose(self):
        a=self.app(age=2.);a.camera.observation[1]['by_jig']['pallet']={'selected':None,'status':'confirming','stable_candidate_seconds':2.4,'stable_candidate_samples':4}
        shown=CameraLifecycle.camera_adoption_results(a,100.)
        self.assertEqual(shown['pallet']['stable_candidate_samples'],4);self.assertIsNone(shown['pallet']['selected'])
        self.assertEqual(a.camera_results(101.01),{});self.assertEqual(CameraLifecycle.camera_adoption_results(a,101.01),{})
    def test_two_second_observation_is_usable_but_three_second_and_old_measurements_are_rejected(self):
        a=self.app(age=2.016);a.measurement_valid_after=98.
        self.assertTrue(a.camera_results(100.)['pallet']['selected'])
        self.assertEqual(a.camera_results(101.1),{})
        a.measurement_valid_after=99.1;self.assertEqual(a.camera_results(100.),{})
    def test_server_transfers_slow_processed_frame_without_retimestamping(self):
        a=self.app(age=2.2);a.camera.preview_observation=None;server=SimpleNamespace(camera=a.camera,generation=0,camera_encode_lock=threading.Lock(),camera_encoded=OrderedDict())
        with patch('so101_teach.remote_server.time.monotonic',return_value=100.):v=Runtime.camera_frame(server)
        self.assertIn('image',v);self.assertEqual(v['at'],97.8);self.assertEqual(v['detection'],a.camera.observation[1])
        with patch('so101_teach.remote_server.time.monotonic',return_value=100.9):v=Runtime.camera_frame(server)
        self.assertNotIn('detection',v)
