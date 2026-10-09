import unittest,time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_ui as fixtures
from so101_teach.domain import JOINTS,Snapshot

class DualEditorTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def open_pair(self):
        a=self.app
        a.step_name.set('첫 번째');a.commit_target();first=a.selected
        a.step_name.set('두 번째');a.commit_target();second=a.selected
        self.root.update();a.steps.selection_set(first);a.select_step();self.root.update()
        box=a.steps.bbox(second);a.steps.event_generate('<Button-3>',x=25,y=box[1]+8);self.root.update()
        return a,a.second_editor,first,second
    def test_right_click_preserves_left_draft_and_both_selections(self):
        a,b,first,second=self.open_pair();self.assertIsNotNone(b)
        self.assertEqual(a.selected,first);self.assertEqual(b.selected,second)
        self.assertEqual(a.step_name.get(),'첫 번째');self.assertEqual(b.edit_title.get(),'스텝 자세 편집 2')
        a.step_name.set('저장 안 한 왼쪽');old=a.target.copy();b.slider_tick(JOINTS[0],b.target[JOINTS[0]]+10);b.step_name.set('오른쪽 변경');b.update_selected_step();self.root.update()
        self.assertEqual(a.target,old);self.assertEqual(a.step_name.get(),'저장 안 한 왼쪽');self.assertEqual(a.selected,first)
        self.assertEqual(a.store.load(next((self.data/'episodes').glob('*.json')))['steps'][1]['name'],'오른쪽 변경')
        self.assertIn('editor2',a.steps.item(second,'tags'))
    def test_close_outside_does_not_save_or_change_left_and_keeps_preview(self):
        a,b,first,second=self.open_pair();before=deepcopy(a.episode);b.step_name.set('버릴 수정')
        a.preview_canvas.event_generate('<Button-3>',x=20,y=20);self.root.update()
        self.assertIsNone(a.second_editor);self.assertEqual(a.episode,before);self.assertEqual(a.selected,first)
        self.assertTrue(a.preview_canvas.winfo_ismapped());self.assertGreater(a.preview_canvas.winfo_width(),350)
    def test_right_edit_execution_uses_only_right_values(self):
        a,b,first,second=self.open_pair();old=a.target.copy();b.slider_tick(JOINTS[0],b.target[JOINTS[0]]+20)
        with patch.object(a,'prepare_execution') as run:b.execute_target()
        self.assertEqual(run.call_args.args[1][0]['ticks'],b.target);self.assertEqual(a.target,old)
    def test_save_right_adds_step_without_left_selection_change(self):
        a,b,first,second=self.open_pair();b.slider_tick('gripper',str(b.target['gripper']+1));b.commit_target();self.root.update()
        self.assertEqual(len(a.episode['steps']),3);self.assertEqual(a.selected,first);self.assertNotEqual(b.selected,second)
    def test_jig_reference_and_live_capture_are_independent(self):
        a,b,first,second=self.open_pair();self.fake_jig_camera();b.follow_jig.set(True);b.update_jig_hint();b.update_selected_step();self.root.update()
        self.assertFalse(a.follow_jig.get());self.assertIn('jig_reference',a.episode['steps'][1]);self.assertFalse(a.episode['steps'][0].get('jig_id'))
        ticks=a.target.copy();ticks[JOINTS[0]]+=15
        a.latest=Snapshot('follower',ticks,{},time.monotonic(),time.time(),a.calibration.sha256,True,'test');b.capture();self.root.update()
        self.assertEqual(a.episode['steps'][-1]['ticks'],ticks);self.assertEqual(a.episode['steps'][-1]['source']['kind'],'measured')
        self.assertEqual(a.selected,first)
    def test_new_episode_and_delete_right_target_close_second_editor(self):
        a,b,first,second=self.open_pair();a.new_episode();self.root.update();self.assertIsNone(a.second_editor)
    def test_safe_boundary_cannot_be_overwritten_from_right(self):
        a,b,first,second=self.open_pair();a.episode['steps'][1]['safe_boundary']='end';b.load(second)
        with self.assertRaises(ValueError):b.update_selected_step()
        self.assertIn('disabled',b.update_step_btn.state())
    def test_shortcuts_route_to_focused_editor(self):
        a,b,first,second=self.open_pair()
        e=SimpleNamespace(widget=b.sliders[JOINTS[0]],state=0)
        a.jig_key_press(e);self.assertTrue(b.follow_jig.get());self.assertFalse(a.follow_jig.get());a.cancel_jig_key()
        with patch.object(b,'capture') as capture:a.space_capture(e)
        capture.assert_called_once()
    def test_stale_right_draft_cannot_overwrite_left_saved_changes(self):
        a,b,first,second=self.open_pair();a.episode['steps'][1]['name']='다른 편집에서 저장'
        with self.assertRaises(ValueError):b.update_selected_step()
        self.assertEqual(a.episode['steps'][1]['name'],'다른 편집에서 저장')
    def test_layout_at_minimum_and_default_size(self):
        a,b,first,second=self.open_pair()
        self.fake_jig_camera();a.follow_jig.set(True);a.update_jig_hint();b.follow_jig.set(True);b.update_jig_hint()
        for size in ('1180x760','1480x920'):
            self.root.geometry(size);self.root.update();a.fit_teach_columns();self.root.update()
            cards=(a.teach_list_card,a.editor_card,b.editor_card,a.preview_card)
            for left,right in zip(cards,cards[1:]):self.assertLessEqual(left.winfo_rootx()+left.winfo_width(),right.winfo_rootx())
            for button in (a.safe_edit_btn,a.delete_step_btn,a.delete_episode_btn):
                self.assertGreaterEqual(button.winfo_width(),button.winfo_reqwidth(),(size,str(button)))
            self.assertGreater(a.preview_canvas.winfo_width(),150)
            for editor in (a,b):
                for widget in (editor.step_jig_choice,editor.move_btn,editor.capture_btn,*editor.spins.values()):
                    self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),editor.editor_card.winfo_rootx()+editor.editor_card.winfo_width(),(size,str(widget)))
                    self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),editor.editor_card.winfo_rooty()+editor.editor_card.winfo_height(),(size,str(widget)))

    def test_reread_uses_jig_in_active_second_editor(self):
        a,b,first,second=self.open_pair();other=a.catalog.duplicate('pallet');a.catalog_changed()
        self.root.update();box=a.steps.bbox(second);a.open_second_editor(SimpleNamespace(y=box[1]+8));b=a.second_editor
        b.set_step_jig(other['id']);b.activate()
        with patch.object(a,'start_camera'):a.request_jig_read()
        self.assertEqual(a.active_jig,other['id']);a.stop_preview(quiet=True)
    def test_click_selected_left_step_returns_preview_without_losing_draft(self):
        a,b,first,second=self.open_pair();a.step_name.set('왼쪽 미저장');box=a.steps.bbox(first)
        a.steps.event_generate('<Button-1>',x=25,y=box[1]+8);self.root.update()
        self.assertEqual(a.preview_editor,1);self.assertEqual(a.step_name.get(),'왼쪽 미저장')

    def test_same_step_right_click_toggles_closed_without_saving(self):
        a,b,first,second=self.open_pair();before=deepcopy(a.episode);b.step_name.set('저장하지 않은 편집 2')
        box=a.steps.bbox(second);a.steps.event_generate('<Button-3>',x=25,y=box[1]+8);self.root.update()
        self.assertIsNone(a.second_editor);self.assertEqual(a.episode,before);self.assertEqual(a.selected,first)
        self.assertEqual(a.steps.item(second,'tags'),'')
    def test_right_selection_uses_color_without_name_marker(self):
        a,b,first,second=self.open_pair()
        self.assertEqual(a.steps.item(second,'values')[0],'02   두 번째');self.assertIn('editor2',a.steps.item(second,'tags'))
        box=a.steps.bbox(first);a.steps.event_generate('<Button-3>',x=25,y=box[1]+8);self.root.update()
        self.assertEqual(b.selected,first);self.assertEqual(a.selected,first)
        from tkinter import ttk
        self.assertEqual(ttk.Style(self.root).lookup('Step.Treeview','background',('selected',)),'#f3e4fc')
        box=a.steps.bbox(first);a.steps.event_generate('<Button-3>',x=25,y=box[1]+8);self.root.update()
        self.assertIsNone(a.second_editor)
