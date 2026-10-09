import unittest,time
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_ui as fixtures
from so101_teach.domain import Snapshot

class SaveDeleteInputTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def event(self,widget=None):return SimpleNamespace(widget=widget or self.app.steps,state=0)
    def test_space_requires_one_second_between_presses(self):
        a=self.app;e=self.event()
        with patch.object(a,'capture') as capture:
            for at,expected in [(10.,1),(10.2,1),(10.999,1),(11.,2)]:
                a.cancel_space_key()
                with patch('so101_teach.workspace_features.time.monotonic',return_value=at):a.space_capture(e)
                self.assertEqual(capture.call_count,expected)
    def test_long_hold_and_x11_repeat_never_add_more_steps(self):
        a=self.app;e=self.event()
        with patch.object(a,'capture') as capture:
            with patch('so101_teach.workspace_features.time.monotonic',return_value=10):a.space_capture(e)
            for at in (10.5,11.,12.,20.):
                a.space_key_release(e)
                with patch('so101_teach.workspace_features.time.monotonic',return_value=at):a.space_capture(e)
            self.assertEqual(capture.call_count,1);a.cancel_space_key()
            with patch('so101_teach.workspace_features.time.monotonic',return_value=21):a.space_capture(e)
            self.assertEqual(capture.call_count,2)
    def test_input_field_does_not_capture_or_start_cooldown(self):
        a=self.app
        with patch.object(a,'capture') as capture:
            a.space_capture(self.event(a.step_name_entry));capture.assert_not_called()
            self.assertFalse(getattr(a,'_space_down',False));a.space_capture(self.event());capture.assert_called_once()
    def test_capture_button_keyboard_cannot_double_save_but_mouse_is_immediate(self):
        a=self.app;a.latest=Snapshot('follower',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        # This fixture has no connected session. Disable unrelated polling so
        # the input test's synthetic enabled button cannot be disabled mid-test.
        self.root.after_cancel(a.job);a.job=None
        b=a.capture_btn;b.state(['!disabled']);b.focus_force();self.root.update()
        b.event_generate('<KeyPress-space>');b.event_generate('<KeyRelease-space>');self.root.update()
        self.assertEqual(len(a.episode['steps']),1)
        b.event_generate('<KeyPress-space>');b.event_generate('<KeyRelease-space>');self.root.update();self.assertEqual(len(a.episode['steps']),1)
        from dataclasses import replace
        a.latest=replace(a.latest,monotonic=time.monotonic());a.latest.ticks['gripper']+=1;b.invoke();self.assertEqual(len(a.episode['steps']),2)
    def make_steps(self):
        a=self.app
        for name in ('A','B','C','D'):a.step_name.set(name);a.commit_target()
        self.root.update();return a,[s['id'] for s in a.episode['steps']]
    def test_repeated_deletion_keeps_same_row_then_previous_until_empty(self):
        a,ids=self.make_steps();a.steps.selection_set(ids[1]);a.select_step();self.root.update()
        for expected_name,expected_id in [('C',ids[2]),('D',ids[3]),('A',ids[0])]:
            a.delete_step_btn.invoke();self.root.update();self.assertEqual(a.selected,expected_id);self.assertEqual(a.steps.selection(),(expected_id,));self.assertEqual(a.step_name.get(),expected_name)
        a.delete_step_btn.invoke();self.root.update();self.assertIsNone(a.selected);self.assertEqual(a.episode['steps'],[])
        a.delete_step_btn.invoke();self.assertEqual(a.episode['steps'],[])
    def test_delete_step_also_shown_on_right_closes_right_and_selects_next(self):
        a,ids=self.make_steps();a.steps.selection_set(ids[1]);a.select_step();self.root.update();box=a.steps.bbox(ids[1]);a.open_second_editor(SimpleNamespace(y=box[1]+8))
        a.delete_step();self.root.update();self.assertIsNone(a.second_editor);self.assertEqual(a.selected,ids[2]);self.assertEqual(a.step_name.get(),'C')
    def test_switching_editors_cannot_bypass_space_cooldown(self):
        a,ids=self.make_steps();box=a.steps.bbox(ids[1]);a.open_second_editor(SimpleNamespace(y=box[1]+8));b=a.second_editor
        with patch.object(a,'capture') as left,patch.object(b,'capture') as right:
            with patch('so101_teach.workspace_features.time.monotonic',return_value=10):a.space_capture(self.event())
            a.cancel_space_key()
            with patch('so101_teach.workspace_features.time.monotonic',return_value=10.5):a.space_capture(self.event(b.sliders['gripper']))
            left.assert_called_once();right.assert_not_called();a.cancel_space_key()
            with patch('so101_teach.workspace_features.time.monotonic',return_value=11):a.space_capture(self.event(b.sliders['gripper']))
            right.assert_called_once()
