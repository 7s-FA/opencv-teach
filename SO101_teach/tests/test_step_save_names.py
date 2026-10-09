import unittest
from copy import deepcopy
from types import SimpleNamespace
from tests import test_ui as fixtures

class StepSaveTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def add(self,name,tick):
        a=self.app;a.step_name.set(name);a.slider_tick('gripper',str(tick));a.commit_target()
    def test_same_name_changed_ticks_appends_and_numbers_without_overwrite(self):
        self.add('놓기',900);first=deepcopy(self.app.episode['steps'][0])
        self.add('놓기',901);self.add('놓기 (1)',902)
        self.assertEqual([s['name'] for s in self.app.episode['steps']],['놓기','놓기 (1)','놓기 (2)'])
        self.assertEqual(self.app.episode['steps'][0],first)
        self.assertEqual(self.app.step_name.get(),'놓기 (2)')
    def test_identical_repeat_is_skipped_but_return_pose_is_allowed(self):
        self.add('놓기',900);self.add('놓기',900);self.assertEqual(len(self.app.episode['steps']),1)
        self.add('놓기',901);self.add('놓기',900);self.assertEqual(len(self.app.episode['steps']),3)
    def test_different_name_or_jig_setting_preserves_intended_step(self):
        self.add('대기',900);self.add('놓기',900);self.fake_jig_camera();self.app.follow_jig.set(True);self.app.commit_target()
        self.assertEqual(len(self.app.episode['steps']),3)
        self.assertEqual(self.app.episode['steps'][-1]['name'],'놓기 (1)')
    def test_explicit_update_still_updates_selected_and_safe_end_stays_last(self):
        self.app.add_safe_steps();self.add('놓기',900);key=self.app.selected
        self.app.slider_tick('gripper','901');self.app.update_selected_step()
        self.assertEqual(len(self.app.episode['steps']),3);self.assertEqual(self.app.selected,key)
        self.add('놓기',902);self.assertEqual(self.app.episode['steps'][-1]['safe_boundary'],'end')
        self.assertEqual(self.app.episode['steps'][-2]['name'],'놓기 (1)')
    def test_second_editor_uses_same_naming_and_duplicate_policy(self):
        self.add('놓기',900);self.root.update();box=self.app.steps.bbox(self.app.selected)
        self.app.open_second_editor(SimpleNamespace(y=box[1]+8));b=self.app.second_editor
        b.commit_target();self.assertEqual(len(self.app.episode['steps']),1)
        b.slider_tick('gripper','901');b.commit_target()
        self.assertEqual(len(self.app.episode['steps']),2);self.assertEqual(b.step_name.get(),'놓기 (1)')
        b.slider_tick('gripper','902');b.update_selected_step();self.assertEqual(len(self.app.episode['steps']),2)
    def test_empty_comparison_remains_accessible_and_clears_previous_values(self):
        p=self.app.jig_comparison;self.root.update();self.assertTrue(p.winfo_ismapped());self.assertTrue(p.empty.winfo_ismapped())
        p.toggle();self.root.update();self.assertTrue(p.toggle_button.winfo_ismapped());self.assertFalse(p.empty.winfo_ismapped())
        p.toggle();self.fake_jig_camera();self.app.follow_jig.set(True);self.add('놓기',900);self.root.update()
        self.assertTrue(p.table.get_children());p.show([], '지그 기준 없음');self.root.update()
        self.assertFalse(p.table.get_children());self.assertEqual(p.choice.get(),'');self.assertTrue(p.empty.winfo_ismapped())
