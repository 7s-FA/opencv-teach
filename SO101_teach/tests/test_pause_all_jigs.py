import time,unittest
from copy import deepcopy
from unittest.mock import patch
from tests import test_ui as fixtures

class PauseAllJigsTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def setup_two(self):
        a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera()
        frame,p,at=a.camera.observation;c=deepcopy(p);c.update(selected=None,status='confirming',pose_measured_at=None)
        a.camera.observation=(frame,{'by_jig':{'pallet':p,other['id']:c},'live_by_jig':{'pallet':p,other['id']:p}},at)
        a.show_page('camera');return other['id'],frame,p
    def test_camera_pause_waits_for_every_registered_jig_before_stopping(self):
        key,frame,p=self.setup_two();a=self.app
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        self.assertEqual(set(a.camera_task['jigs']),{'pallet',key});self.assertTrue(a.camera_task['pause_all'])
        self.assertEqual(a.jig_pause_btn['text'],'전체 지그 채택 중')
        first=deepcopy(p);first['pose_measured_at']=time.monotonic()
        a.camera.observation=(frame,{'by_jig':{'pallet':first,key:{'selected':None}}},time.monotonic())
        a.poll_camera_lifecycle();self.assertIsNotNone(a.camera_task)
        second=deepcopy(first);second['pose_measured_at']=time.monotonic()
        a.camera.observation=(frame,{'by_jig':{'pallet':first,key:second}},time.monotonic())
        a.poll_camera_lifecycle();self.assertIsNone(a.camera_task);self.assertTrue(a.jig_updates_paused)
        self.assertEqual(set(a.teaching_jig_results),{'pallet',key})
    def test_failed_all_jig_adoption_resumes_updates_without_partial_success(self):
        key,frame,p=self.setup_two();a=self.app
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        a.camera_task['budget_started']-=20;a.camera.observation=None;a.poll_camera_lifecycle()
        self.assertIsNone(a.camera_task);self.assertFalse(a.jig_updates_paused);self.assertFalse(a.teaching_jig_results)
        self.assertIn('자동 갱신을 계속',a.message.get())
    def test_already_adopted_all_jigs_pause_without_discarding_them(self):
        key,frame,p=self.setup_two();a=self.app
        a.camera.observation=(frame,{'by_jig':{'pallet':p,key:deepcopy(p)}},time.monotonic())
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_not_called();self.assertIsNone(a.camera_task);self.assertEqual(set(a.teaching_jig_results),{'pallet',key})

    def test_camera_start_failure_does_not_leave_partial_pause(self):
        self.setup_two();a=self.app
        with patch.object(a,'start_camera',side_effect=ValueError('camera unavailable')):
            with self.assertRaisesRegex(ValueError,'camera unavailable'):a.toggle_jig_updates()
        self.assertIsNone(a.camera_task);self.assertFalse(a.jig_updates_paused);self.assertFalse(a.teaching_jig_results)
