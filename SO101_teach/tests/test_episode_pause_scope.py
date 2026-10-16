import time,unittest
from copy import deepcopy
from unittest.mock import patch
from tests import test_pause_all_jigs as fixtures

class EpisodePauseScopeTests(unittest.TestCase):
    setUp=fixtures.PauseAllJigsTests.setUp
    tearDown=fixtures.PauseAllJigsTests.tearDown
    fake_jig_camera=fixtures.PauseAllJigsTests.fake_jig_camera
    setup_two=fixtures.PauseAllJigsTests.setup_two

    def episode(self,keys):
        a=self.app
        a.episode['steps']=[{**a.store.step(a.target,'step'), 'jig_id':key,'jig_reference':{'pose':[228,-138,67],'symmetry_deg':90,'stl_sha256':a.catalog.mesh(key)['sha256']}} if key else a.store.step(a.target,'fixed') for key in keys]

    def test_whole_episode_waits_for_carrier_even_when_viewing_pallet_step(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet',None,key,'pallet'])
        a.show_page('teach');a.set_step_jig('pallet');a.selected=a.episode['steps'][0]['id'];a.camera_sleeping=False
        before=deepcopy(a.episode)
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        self.assertEqual(a.camera_task['jigs'],['pallet',key]);self.assertFalse(a.jig_updates_paused)
        self.assertEqual(a.jig_pause_btn['text'],'에피소드 지그 채택 중')
        first=deepcopy(p);first['pose_measured_at']=time.monotonic()
        a.camera.observation=(frame,{'by_jig':{'pallet':first,key:{'selected':None}}},time.monotonic())
        a.poll_camera_lifecycle();self.assertFalse(a.jig_updates_paused)
        a.camera.observation=(frame,{'by_jig':{'pallet':first,key:deepcopy(first)}},time.monotonic())
        a.poll_camera_lifecycle();self.assertTrue(a.jig_updates_paused)
        self.assertEqual(set(a.teaching_jig_results),{'pallet',key});self.assertEqual(a.episode,before)

    def test_camera_filter_does_not_narrow_episode_scope(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet',key]);a.camera_all.set(False);a.active_jig='pallet'
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        self.assertEqual(a.camera_task['jigs'],['pallet',key]);self.assertTrue(a.camera_task['pause_episode'])

    def test_unused_registered_or_editor_jig_does_not_block_episode_pause(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet']);a.show_page('teach');a.set_step_jig(key)
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_not_called();self.assertTrue(a.jig_updates_paused);self.assertEqual(set(a.teaching_jig_results),{'pallet'})

    def test_episode_measurement_failure_keeps_updates_running(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet',key])
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        a.camera_task['budget_started']-=30;a.camera.observation=None;a.poll_camera_lifecycle()
        self.assertFalse(a.jig_updates_paused);self.assertFalse(a.teaching_jig_results);self.assertIsNone(a.camera_task)

    def test_fixed_only_episode_does_not_wait_for_any_registered_jig(self):
        key,frame,p=self.setup_two();a=self.app;self.episode([None]);a.camera=None
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_not_called();self.assertTrue(a.jig_updates_paused);self.assertFalse(a.teaching_jig_results)

    def test_immediate_pause_keeps_already_adopted_unused_jig(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet'])
        other=deepcopy(p);other['selected']['metric']['center_xy_mm']=[300,50]
        a.camera.observation=(frame,{'by_jig':{'pallet':p,key:other}},time.monotonic())
        before=deepcopy(a.episode)
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_not_called();self.assertEqual(set(a.teaching_jig_results),{'pallet',key})
        with patch('so101_teach.camera_lifecycle.time.monotonic',return_value=time.monotonic()+100):
            self.assertEqual(a.current_jig_reference(key)['pose'][:2],[300,50])
        self.assertEqual(a.episode,before)

    def test_pause_wait_preserves_unused_adoption_from_before_request(self):
        key,frame,p=self.setup_two();a=self.app;self.episode([key])
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        self.assertEqual(a.camera_task['jigs'],[key]);self.assertFalse(a.jig_updates_paused)
        self.assertIn('pallet',a.camera_task['pause_adopted'])
        current=deepcopy(p);current['pose_measured_at']=time.monotonic()
        a.camera.observation=(frame,{'by_jig':{key:current}},time.monotonic())
        a.poll_camera_lifecycle();self.assertTrue(a.jig_updates_paused)
        self.assertEqual(set(a.teaching_jig_results),{'pallet',key})

    def test_unused_jig_adopted_during_wait_is_retained_when_required_one_arrives(self):
        key,frame,p=self.setup_two();a=self.app;self.episode([key]);a.camera.observation=None
        with patch.object(a,'start_camera'):a.toggle_jig_updates()
        current=deepcopy(p);current['pose_measured_at']=time.monotonic()
        a.camera.observation=(frame,{'by_jig':{'pallet':current,key:{'selected':None}}},time.monotonic())
        a.poll_camera_lifecycle();self.assertFalse(a.jig_updates_paused)
        a.camera.observation=(frame,{'by_jig':{'pallet':{'selected':None},key:current}},time.monotonic())
        a.poll_camera_lifecycle();self.assertTrue(a.jig_updates_paused)
        self.assertEqual(set(a.teaching_jig_results),{'pallet',key})

    def test_expired_unused_detection_is_not_revived_as_an_adoption(self):
        key,frame,p=self.setup_two();a=self.app;self.episode(['pallet'])
        expired=deepcopy(p);expired['pose_measured_at']=time.monotonic()-a.pose_latch.seconds-1
        a.camera.observation=(frame,{'by_jig':{'pallet':p,key:expired}},time.monotonic())
        a.toggle_jig_updates();self.assertEqual(set(a.teaching_jig_results),{'pallet'})
