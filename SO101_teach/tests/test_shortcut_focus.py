import unittest,time
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests import test_ui as fixtures
class ShortcutFocusTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def key(self,widget,key):
        widget.focus_force();self.root.update();widget.event_generate('<KeyPress-'+key+'>');widget.event_generate('<KeyRelease-'+key+'>');self.root.update()
    def test_space_on_checkbox_saves_without_toggling(self):
        a=self.app;a.follow_jig.set(True)
        with patch.object(a,'capture') as capture:self.key(a.follow_jig_check,'space');capture.assert_called_once()
        self.assertTrue(a.follow_jig.get())
    def test_space_on_action_button_does_not_invoke_action(self):
        a=self.app;action=Mock();a.delete_step_btn.configure(command=action)
        with patch.object(a,'capture') as capture:self.key(a.delete_step_btn,'space');capture.assert_called_once()
        action.assert_not_called()
    def test_readonly_combo_space_saves_without_changing_choice(self):
        a=self.app;index=a.step_jig_choice.current()
        with patch.object(a,'capture') as capture:self.key(a.step_jig_choice,'space');capture.assert_called_once()
        self.assertEqual(a.step_jig_choice.current(),index)
    def test_title_space_is_text_but_enter_finishes_editing(self):
        a=self.app;a.step_name.set('제목')
        with patch.object(a,'capture') as capture:
            self.key(a.step_name_entry,'space');capture.assert_not_called();self.assertIn(' ',a.step_name.get())
            self.key(a.step_name_entry,'Return');self.assertEqual(self.root.focus_get(),a.editor_card)
            self.key(a.editor_card,'space');capture.assert_called_once()
    def test_background_click_exits_title_without_erasing_it(self):
        a=self.app;a.step_name.set('내 제목');a.step_name_entry.focus_force();self.root.update()
        a.preview_canvas.event_generate('<ButtonPress-1>',x=20,y=20);a.preview_canvas.event_generate('<ButtonRelease-1>',x=20,y=20);self.root.update()
        self.assertEqual(a.step_name.get(),'내 제목');self.assertNotEqual(self.root.focus_get(),a.step_name_entry)
    def test_combo_selection_releases_focus_in_both_editors(self):
        a=self.app;a.commit_target();self.root.update();a.open_second_editor(SimpleNamespace(y=a.steps.bbox(a.selected)[1]+4));b=a.second_editor
        for editor in (a,b):
            editor.step_jig_choice.focus_force();self.root.update();editor.step_jig_choice.event_generate('<<ComboboxSelected>>');self.root.update()
            self.assertEqual(self.root.focus_get(),editor.editor_card)
        b.follow_jig.set(True)
        with patch.object(b,'capture') as capture:self.key(b.follow_jig_check,'space');capture.assert_called_once()
        self.assertTrue(b.follow_jig.get())
    def test_other_page_and_numeric_input_keep_native_behavior(self):
        a=self.app
        with patch.object(a,'capture') as capture:
            self.key(a.spins['shoulder_pan'],'space');capture.assert_not_called()
            a.show_page('camera');self.key(a.camera_canvas,'space');capture.assert_not_called()

    def test_click_checkbox_finishes_title_and_does_not_toggle_again_on_space(self):
        a=self.app;a.step_name_entry.focus_force();self.root.update();w=a.follow_jig_check
        w.event_generate('<ButtonPress-1>',x=8,y=8);w.event_generate('<ButtonRelease-1>',x=8,y=8);self.root.update()
        self.assertTrue(a.follow_jig.get());self.assertEqual(self.root.focus_get(),a.editor_card)
        with patch.object(a,'capture') as capture:self.key(a.editor_card,'space');capture.assert_called_once()
        self.assertTrue(a.follow_jig.get())
    def test_episode_title_enter_exits_editing_without_changing_name(self):
        a=self.app
        def walk(w):
            yield w
            for child in w.winfo_children():yield from walk(child)
        entry=next(w for w in walk(self.root) if w.winfo_class()=='TEntry' and str(w.cget('textvariable'))==str(a.episode_name))
        a.episode_name.set('새 에피소드 제목');self.key(entry,'Return')
        self.assertEqual(a.episode_name.get(),'새 에피소드 제목');self.assertEqual(self.root.focus_get(),a.editor_card)

    def test_camera_choice_returns_focus_without_teaching_shortcut(self):
        a=self.app;a.show_page('camera');w=a.camera_jig_choice;w.focus_force();self.root.update()
        w.event_generate('<<ComboboxSelected>>');self.root.update();self.assertNotEqual(self.root.focus_get(),w)
        with patch.object(a,'capture') as capture:self.key(self.root.focus_get(),'space');capture.assert_not_called()
    def test_settings_entry_enter_and_blank_click_do_not_save_configuration(self):
        a=self.app;a.show_page('settings')
        def walk(w):
            yield w
            for c in w.winfo_children():yield from walk(c)
        entry=next(w for w in walk(a.settings.pages['robot']) if w.winfo_class()=='TEntry')
        with patch.object(a.settings,'save_profile') as save:
            self.key(entry,'Return');self.assertNotEqual(self.root.focus_get(),entry);save.assert_not_called()
