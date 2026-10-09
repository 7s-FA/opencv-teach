import json,time,unittest
from copy import deepcopy
from unittest.mock import patch
from tests.test_current_carrier_preview import CurrentCarrierPreviewTests
from tests import test_ui as fixtures
from so101_teach.domain import ROOT
from so101_teach.jig_compatibility import ASSEMBLED_CARRIER_SHA

class AuditUITests(unittest.TestCase):
    setUp=CurrentCarrierPreviewTests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def test_old_episode_enters_preview_and_records_the_compatible_basis(self):
        from tests.fixtures import legacy_carrier_episode
        a=self.app;a.episode=legacy_carrier_episode();before=deepcopy(a.episode)
        current={}
        for step in a.episode['steps']:
            key=step.get('jig_id')
            if key and key not in current:
                current[key]=deepcopy(step['jig_reference'])
                if key!='pallet':current[key].update(symmetry_deg=360,stl_sha256=ASSEMBLED_CARRIER_SHA,mesh_yaw_offset_deg=180.)
        with patch.object(a,'motion_request') as motion:a.begin_execution('preview',a.episode['steps'],current)
        motion.assert_not_called();self.assertTrue(a.playing);self.assertEqual(a.episode,before)
        self.assertEqual(sum(v.get('jig_reference_compatibility')=='legacy_carrier_to_assembly' for v in a.last_plan),12)
        record=json.loads(next((self.data/'episode_runs'/a.episode['id']).glob('*.json')).read_text())
        self.assertEqual(record['state'],'preview')
        self.assertTrue(any(g.get('execution_reference',{}).get('stl_sha256')==ASSEMBLED_CARRIER_SHA for g in record['groups']))
        a.stop_preview(quiet=True)
    def test_resume_clears_exhausted_acquisition_and_old_teaching_hold(self):
        a=self.app;a.pose_latch.stability.clear(0);a.pose_latch.stability.finish(9)
        self.assertTrue(a.pose_latch.stability.exhausted);a.jig_updates_paused=True
        a.toggle_jig_updates();self.assertFalse(a.jig_updates_paused);self.assertFalse(a.pose_latch.stability.exhausted)
        self.assertEqual(a.teaching_jig_results,{});self.assertGreater(a.measurement_valid_after,0)
    def test_camera_failure_cannot_complete_a_read_even_with_a_fresh_selected_pose(self):
        a=self.app;self.fake_jig_camera()
        with patch.object(a,'start_camera'),patch.object(a,'suspend_camera'):
            a.request_jig_read();frame,result,_=a.camera.observation;result['pose_measured_at']=time.monotonic();a.camera.observation=(frame,result,time.monotonic());a.camera.error='camera failed'
            a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);self.assertIn('camera failed',a.message.get())
            self.assertNotIn('읽기 완료',a.message.get())
