import unittest,time,threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch,Mock
from tests import test_ui as fixtures

class StepMoveHoldTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def fake_jig_camera(self,*args,**kwargs):
        fixtures.UITests.fake_jig_camera(self,*args,**kwargs)
        # A fresh frame represents the completed asynchronous camera reopen.
        self.app.camera_sleeping=False;self.app.camera_close_requested=False;self.app.camera_restart_pending=False
    def teach(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True);a.commit_target();return a,deepcopy(a.episode['steps'][0])
    def test_first_move_latches_current_and_repeated_moves_ignore_new_frames_and_expiry(self):
        a,step=self.teach();before=deepcopy(a.episode)
        with patch.object(a,'begin_execution') as run,patch.object(a,'start_camera') as start:
            a.prepare_execution('move',[step]);pose=run.call_args.args[2]['pallet'];self.assertTrue(a.jig_updates_paused)
            self.fake_jig_camera((260,-90,75))
            with patch('so101_teach.camera_lifecycle.time.monotonic',return_value=time.monotonic()+300):a.prepare_execution('move',[step])
            self.assertEqual(run.call_args.args[2]['pallet'],pose);self.assertEqual(run.call_count,2);start.assert_not_called()
        self.assertEqual(a.episode,before)
    def test_missing_current_reads_once_then_next_move_uses_latched_pose(self):
        a,step=self.teach();a.camera.observation=None
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);self.assertTrue(a.pending_execution['hold_for_moves']);run.assert_not_called()
            self.fake_jig_camera((240,-125,70));a.check_pending_execution();self.assertEqual(run.call_count,1)
            a.stop_preview(quiet=True);a.camera.observation=None
            a.prepare_execution('move',[step]);self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args.args[2]['pallet']['pose'],[240,-125,70]);self.assertIsNone(a.pending_execution)
    def test_manual_reread_replaces_held_position_without_changing_teaching_basis(self):
        a,step=self.teach();basis=deepcopy(step['jig_reference'])
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);a.request_jig_read();self.fake_jig_camera((245,-115,72));a.poll_camera_lifecycle()
            a.prepare_execution('move',[step]);self.assertEqual(run.call_args.args[2]['pallet']['pose'],[245,-115,72])
        self.assertEqual(a.ensure_teaching_reference(),basis)
    def test_different_jig_gets_own_first_read_and_retains_other_jig(self):
        a,step=self.teach();other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera()
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);old=deepcopy(a.teaching_jig_results['pallet'])
            next_step=deepcopy(step);next_step['jig_id']=other['id']
            a.prepare_execution('move',[next_step]);self.assertEqual(a.active_jig,other['id']);self.assertIsNotNone(a.pending_execution)
            self.fake_jig_camera((300,-60,20));a.check_pending_execution()
            self.assertEqual(run.call_args.args[2][other['id']]['pose'],[300,-60,20]);self.assertEqual(a.teaching_jig_results['pallet'],old)
    def test_paused_preview_reuses_hold_but_full_episode_requires_new_measurement(self):
        a,step=self.teach()
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);held=deepcopy(run.call_args.args[2]);run.reset_mock()
            a.prepare_execution('preview',[step]);run.assert_called_once();self.assertEqual(run.call_args.args[2],held)
            run.reset_mock();a.prepare_execution('play',[step]);a.check_pending_execution();run.assert_not_called()
            self.assertIsNotNone(a.pending_execution);a.stop_preview(quiet=True)
    def test_failed_or_cancelled_first_measurement_never_uses_saved_teaching_pose(self):
        a,step=self.teach();a.camera.observation=None
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);a.pending_execution['started']-=10;a.pending_execution['budget_started']-=10;a.check_pending_execution()
            run.assert_not_called();self.assertFalse(a.teaching_jig_results)
            a.prepare_execution('move',[step]);a.stop_preview(quiet=True);self.fake_jig_camera();a.check_pending_execution();run.assert_not_called()
    def test_geometry_invalidation_forces_new_measurement(self):
        a,step=self.teach()
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('move',[step]);a.invalidate_jig_measurements();a.camera.observation=None;run.reset_mock()
            a.prepare_execution('move',[step]);run.assert_not_called();self.assertIsNotNone(a.pending_execution)
    def test_held_pose_stays_visible_during_motion_even_without_camera_frame(self):
        a,step=self.teach()
        with patch.object(a,'begin_execution'):a.prepare_execution('move',[step])
        old=a.current_jig_reference();a.camera.observation=None;a.detector.freeze(True)
        a.session=SimpleNamespace(program_active=threading.Event());a.session.program_active.set()
        try:self.assertEqual(a.current_jig_reference(),old)
        finally:a.session=None;a.detector.freeze(False)
    def test_two_editors_use_same_held_pose(self):
        a,step=self.teach();a.steps.selection_set(step['id']);a.select_step();self.root.update();box=a.steps.bbox(step['id']);a.open_second_editor(SimpleNamespace(y=box[1]+5))
        with patch.object(a,'begin_execution') as run:
            a.execute_target();first=deepcopy(run.call_args.args[2]);self.fake_jig_camera((260,-90,75));a.second_editor.execute_target()
            self.assertEqual(run.call_count,2);self.assertEqual(run.call_args.args[2],first)
    def test_pi_plan_receives_held_pose_on_each_move(self):
        a,step=self.teach();calls=[]
        class Link:
            error=None
            def close(self,**kwargs):pass
            def rpc(self,*args):pass
        a.remote_mode=True;a.remote=Link()
        with patch.object(a.settings.pool,'submit',side_effect=lambda *args:(calls.append(args),Mock())[1]):
            a.prepare_execution('move',[step]);first=deepcopy(calls[-1][2]['current']);a.remote_plan_job=None;a.stop_preview(quiet=True)
            self.fake_jig_camera((270,-70,75));a.prepare_execution('move',[step]);self.assertEqual(calls[-1][2]['current'],first)
        a.remote_plan_job=None;a.remote=None;a.remote_mode=False;a.stop_preview(quiet=True)
