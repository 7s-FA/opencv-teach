import unittest
from copy import deepcopy
from unittest.mock import patch
from . import test_ui as fixtures

class StepReferenceTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def saved_step(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target()
        a.steps.selection_set(a.selected);a.select_step();return a

    def test_selected_step_shows_its_saved_basis_without_camera_or_motion(self):
        a=self.saved_step();s=a.episode['steps'][0];s['jig_reference']['pose']=[210,-110,15]
        before=deepcopy(a.episode);a.camera=None
        with patch.object(a,'current_jig_reference',side_effect=AssertionError('No measurement')),patch.object(a,'motion_request',side_effect=AssertionError('No movement')):
            a.select_step()
        text=a.jig_hint.get();self.assertIn('저장 초기 기준',text)
        self.assertIn('X→110.0 · Y↑210.0 mm · 15.0°',text);self.assertEqual(a.episode,before)

    def test_new_camera_measurements_do_not_change_saved_display(self):
        a=self.saved_step();before=a.jig_hint.get();episode=deepcopy(a.episode)
        self.fake_jig_camera((250,-90,80));a.update_live_jig_comparison();a.update_jig_hint()
        self.assertEqual(a.jig_hint.get(),before);self.assertEqual(a.episode,episode)

    def test_switching_steps_uses_each_individual_reference(self):
        a=self.saved_step();first=a.selected;other=deepcopy(a.episode['steps'][0]);other['id']='other';other['jig_reference']['pose']=[200,-100,30];a.episode['steps'].append(other);a.refresh_steps()
        a.steps.selection_set('other');a.select_step();self.assertIn('X→100.0 · Y↑200.0 mm · 30.0°',a.jig_hint.get())
        a.steps.selection_set(first);a.select_step();self.assertIn('X→138.0 · Y↑228.0 mm · 67.0°',a.jig_hint.get())

    def test_new_step_and_fixed_step_labels_are_distinct(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True);a.update_jig_hint();self.assertIn('새 스텝 기준',a.jig_hint.get())
        a.follow_jig.set(False);a.commit_target();a.steps.selection_set(a.selected);a.select_step()
        self.assertIn('보정 안 함',a.jig_hint.get());self.assertNotIn('저장 초기 기준',a.jig_hint.get())
