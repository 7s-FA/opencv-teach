import time,unittest
from unittest.mock import Mock,patch
from threading import Event
from tests import test_ui as fixtures
from so101_teach.motion import MotionSession
from so101_teach.domain import Snapshot,JOINTS

class LiveAdjustTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        self.app.live_adjust.stop(halt=False);self.app.session=None
        fixtures.UITests.tearDown(self)
    def arm(self):
        a=self.app;a.commit_target()
        s=MotionSession('unused',a.calibration);s.running=True;s.state='HOLD';a.session=s
        a.latest=Snapshot('follower',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        self.calls=[]
        def request(action,targets=None):
            self.calls.append((action,targets));s.request_serial+=1
            if action=='move':s.command_pending.set();s.program_active.set();s.state='MOVING'
            if action=='hold':s.command_pending.clear();s.program_active.clear();s.state='HOLD'
            return s.request_serial
        s.request=Mock(side_effect=request);a.live_adjust.toggle(a);return a,s
    def finish(self,a,s):
        s.completed_request_id=a.live_adjust.request_id;s.command_pending.clear();s.program_active.clear();s.state='HOLD';a.live_adjust.poll()
    def test_initial_move_and_bounded_keyboard_latest_only(self):
        a,s=self.arm();n=JOINTS[0];base=a.target[n]
        a.tick_vars[n].set(str(base+300));a.input_tick(n)
        self.assertEqual(a.target[n],base+50);self.assertEqual(a.tick_vars[n].get(),str(base+50));self.assertEqual(len(self.calls),1)
        a.tick_vars[n].set(str(base-300));a.input_tick(n)
        self.assertEqual(a.target[n],base-50);self.finish(a,s)
        self.assertEqual(len(self.calls),2);self.assertEqual(self.calls[-1][1][0][n],base-50)
    def test_partial_typing_does_not_move_and_slider_is_disabled(self):
        a,s=self.arm();before=a.target.copy();n=JOINTS[1]
        a.tick_vars[n].set('2');a.slider_tick(n,100);self.root.update_idletasks()
        self.assertEqual(a.target,before);self.assertEqual(len(self.calls),1);self.assertIn('disabled',a.sliders[n].state())
        a.tick_vars[n].set('bad');a.input_tick(n);self.assertEqual(a.tick_vars[n].get(),str(before[n]))
    def test_toggle_off_holds_and_discards_pending_edit(self):
        a,s=self.arm();n=JOINTS[0];a.tick_vars[n].set(str(a.target[n]+10));a.input_tick(n)
        a.live_adjust.toggle(a);self.assertIsNone(a.live_adjust.owner);self.assertEqual(self.calls[-1][0],'hold')
        a.live_adjust.poll();self.assertEqual([c[0] for c in self.calls],['move','hold']);self.assertNotIn('disabled',a.sliders[n].state())
    def test_navigation_and_step_selection_stop_live_adjust(self):
        a,s=self.arm();a.show_page('devices');self.assertIsNone(a.live_adjust.owner)
        a.show_page('teach');a.live_adjust.toggle(a);a.step_name.set('다음');a.target[JOINTS[0]]+=1;a.commit_target()
        a.live_adjust.poll();self.assertIsNone(a.live_adjust.owner)
    def test_right_editor_is_independent_and_other_editor_cancels(self):
        a,s=self.arm();a.live_adjust.stop();a.open_second_editor(type('E',(),{'y':a.steps.bbox(a.selected)[1]+4})())
        b=a.second_editor;before=a.target.copy();a.live_adjust.toggle(b)
        b.tick_vars['gripper'].set(str(b.target['gripper']+900));b.input_tick('gripper')
        self.assertEqual(a.target,before);self.assertLessEqual(abs(b.target['gripper']-before['gripper']),50)
        a.input_tick(JOINTS[0]);self.assertIsNone(a.live_adjust.owner)
    def test_pending_plan_is_cancelled_and_cannot_dispatch_after_loss(self):
        a,s=self.arm();a.live_adjust.stop();s.state='HOLD'
        future=Mock();future.cancel=Mock()
        def waiting():a.remote_plan_job=(future,(),a.episode['id'])
        with patch.object(a,'execute_target',side_effect=waiting):a.live_adjust.toggle(a)
        self.assertTrue(a.live_adjust.waiting);s.running=False;a.live_adjust.poll()
        self.assertIsNone(a.remote_plan_job);self.assertIsNone(a.live_adjust.owner);future.cancel.assert_called_once()
    def test_internal_camera_navigation_keeps_toggle_manual_navigation_stops(self):
        a,s=self.arm();a.show_page('camera',internal=True);self.assertIs(a.live_adjust.owner,a)
        a.show_page('teach',internal=True);self.assertIs(a.live_adjust.owner,a)
        a.show_page('settings');self.assertIsNone(a.live_adjust.owner)
    def test_read_only_cannot_toggle_and_completed_move_can_advance_one_tick(self):
        a,s=self.arm();self.finish(a,s);n=JOINTS[0];before=a.target[n]
        a.tick_vars[n].set(str(before+1));a.input_tick(n);self.assertEqual(self.calls[-1][1][0][n],before+1)
        a.live_adjust.stop();s.state='READ_ONLY'
        with self.assertRaises(ValueError):a.live_adjust.toggle(a)
    def test_brief_feedback_gap_keeps_mode_and_waits_to_send_latest_edit(self):
        from dataclasses import replace
        a,s=self.arm();self.finish(a,s);fresh=a.latest
        a.latest=replace(fresh,monotonic=time.monotonic()-.8)
        a.live_adjust.poll();self.assertIs(a.live_adjust.owner,a)
        before=len(self.calls);n=JOINTS[0];a.tick_vars[n].set(str(a.target[n]+10));a.input_tick(n)
        self.assertEqual(len(self.calls),before)
        a.latest=replace(fresh,monotonic=time.monotonic());a.live_adjust.poll()
        self.assertIs(a.live_adjust.owner,a);self.assertEqual(len(self.calls),before+1)
    def test_fresh_session_feedback_wins_over_stale_monitor_and_long_gap_stops(self):
        from dataclasses import replace
        a,s=self.arm();s.latest=a.latest;a.latest=replace(a.latest,monotonic=time.monotonic()-3)
        a.live_adjust.poll();self.assertIs(a.live_adjust.owner,a)
        s.latest=replace(s.latest,monotonic=time.monotonic()-2.1);a.live_adjust.poll()
        self.assertIsNone(a.live_adjust.owner);self.assertEqual(self.calls[-1][0],'hold')
    def test_initial_jig_measurement_is_not_frozen_by_live_adjust_toggle(self):
        a,s=self.arm();self.finish(a,s)
        a.pending_execution={'action':'move','steps':a.episode['steps'],'started':time.monotonic(),'page':'teach'}
        a.root.after_cancel(a.job)
        with patch.object(a,'check_pending_execution'),patch.object(a,'poll_camera_lifecycle'):
            a.poll()
        self.assertIs(a.live_adjust.owner,a)
        self.assertFalse(a.detector.frozen)
        a.pending_execution=None
        a.root.after_cancel(a.job)
        with patch.object(a,'check_pending_execution'),patch.object(a,'poll_camera_lifecycle'):
            a.poll()
        self.assertTrue(a.detector.frozen)
