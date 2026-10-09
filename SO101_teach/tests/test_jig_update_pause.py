import time,threading,unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch,Mock
from tests import test_ui as fixtures

class JigUpdatePauseTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def pause(self):
        self.fake_jig_camera();a=self.app;old=a.current_jig_reference();a.toggle_jig_updates();return a,old
    def test_pause_survives_new_frames_and_expiry_for_first_and_later_saved_steps(self):
        a,old=self.pause();self.fake_jig_camera((260,-120,74));a.suspend_camera()
        with patch('so101_teach.camera_lifecycle.time.monotonic',return_value=time.monotonic()+300):
            self.assertEqual(a.current_jig_reference(),old)
            a.follow_jig.set(True);a.commit_target();a.slider_tick('gripper',str(a.target['gripper']+1));a.commit_target()
        self.assertEqual([s['jig_reference'] for s in a.episode['steps']],[old,old])
        self.assertFalse(a.detector.frozen)
    def test_resume_uses_live_results_and_restores_normal_button(self):
        a,old=self.pause();self.fake_jig_camera((260,-120,74));a.toggle_jig_updates()
        self.assertFalse(a.jig_updates_paused);self.assertNotEqual(a.current_jig_reference(),old)
        self.assertEqual(a.jig_pause_btn['text'],'자동 갱신 정지')
    def test_missing_pose_requests_one_measurement_then_holds(self):
        a=self.app
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_called_once();self.assertTrue(a.jig_updates_paused);self.assertIsNone(a.current_jig_reference())
    def test_manual_reread_updates_hold_but_does_not_overwrite_saved_episode_basis(self):
        a,old=self.pause();a.follow_jig.set(True);a.commit_target();a.request_jig_read()
        self.fake_jig_camera((260,-120,74));a.poll_camera_lifecycle()
        self.assertTrue(a.jig_updates_paused);self.assertEqual(a.current_jig_reference()['pose'],[260,-120,74])
        self.assertEqual(a.ensure_teaching_reference(),old);a.commit_target()
        self.assertEqual(a.episode['steps'][0]['jig_reference'],old)
        self.assertEqual(a.episode['steps'][-1]['jig_reference']['pose'],[260,-120,74])
    def test_failed_reread_preserves_hold(self):
        a,old=self.pause();a.request_jig_read();a.camera.observation=None;a.camera_task['started']-=10;a.camera_task['budget_started']-=10
        a.poll_camera_lifecycle();self.assertEqual(a.current_jig_reference(),old)
    def test_execution_requires_new_pose_even_when_paused(self):
        a,old=self.pause();a.follow_jig.set(True);a.commit_target()
        with patch.object(a,'begin_execution') as begin:
            a.prepare_execution('play',deepcopy(a.episode['steps']));a.check_pending_execution();begin.assert_not_called()
            self.fake_jig_camera((260,-120,74));a.check_pending_execution();begin.assert_called_once()
            self.assertEqual(begin.call_args.args[2]['pallet']['pose'],[260,-120,74])
        a.stop_preview(quiet=True);self.assertEqual(a.current_jig_reference(),old)
    def test_failed_execution_measurement_never_falls_back_to_held_teaching_pose(self):
        a,old=self.pause();a.follow_jig.set(True);a.commit_target()
        with patch.object(a,'begin_execution') as begin:
            a.prepare_execution('play',deepcopy(a.episode['steps']));a.camera.observation=None;a.pending_execution['started']-=10;a.pending_execution['budget_started']-=10
            a.check_pending_execution();begin.assert_not_called()
        self.assertEqual(a.current_jig_reference(),old)
    def test_jigs_hold_independent_positions_and_allow_selection_changes(self):
        a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera()
        frame,result,at=a.camera.observation;second=deepcopy(result);second['selected']['metric']['center_xy_mm']=[300,-50]
        a.camera.observation=(frame,{'by_jig':{'pallet':result,other['id']:second}},at);a.toggle_jig_updates()
        a.select_camera_jig(other['id']);self.assertEqual(a.current_jig_reference(other['id'])['pose'][:2],[300,-50])
        self.assertEqual(a.current_jig_reference('pallet')['pose'][:2],[228,-138])
    def test_geometry_change_invalidates_held_pose_without_resuming_auto_updates(self):
        a,old=self.pause();a.invalidate_jig_measurements()
        self.assertTrue(a.jig_updates_paused);self.assertIsNone(a.current_jig_reference())
    def test_pause_does_not_block_leader_teaching(self):
        self.fake_jig_camera();a=self.app;a.session=SimpleNamespace(state='FOLLOW',program_active=threading.Event())
        try:a.toggle_jig_updates();self.assertTrue(a.jig_updates_paused)
        finally:a.session=None
    def test_pause_button_is_left_of_reread_and_all_buttons_fit(self):
        a,old=self.pause()
        for size in ('1180x760','1480x920'):
            self.root.geometry(size);self.root.update()
            left=a.jig_pause_btn;right=a.teach_reread_jig_btn
            self.assertLessEqual(left.winfo_rootx()+left.winfo_width(),right.winfo_rootx())
            self.assertGreaterEqual(left.winfo_width(),left.winfo_reqwidth())
            self.assertLessEqual(a.follow_btn.winfo_rootx()+a.follow_btn.winfo_width(),left.winfo_rootx())
            self.assertLessEqual(right.winfo_rootx()+right.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
    def test_pause_disabled_during_measurement_or_execution(self):
        a,old=self.pause();a.pending_execution={'started':time.monotonic()};a.update_jig_pause_button()
        self.assertTrue(a.jig_pause_btn.instate(['disabled']))
        with self.assertRaises(ValueError):a.toggle_jig_updates()
        a.pending_execution=None
    def test_teaching_pause_reads_selected_step_jig_when_only_other_jig_is_known(self):
        a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera()
        a.set_step_jig(other['id']);a.toggle_jig_updates()
        self.assertEqual(a.camera_task['jig'],other['id']);self.assertEqual(a.active_jig,other['id'])
        self.assertIn('pallet',a.teaching_jig_results);self.assertNotIn(other['id'],a.teaching_jig_results)
