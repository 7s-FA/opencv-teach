"""Draft recovery and navigation without devices or live project data."""
import unittest
from tests import test_ui as fixtures

class UsabilityTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def steps(self):
        a=self.app;a.commit_target();first=a.selected;a.selected=None;a.step_name.set('두 번째');a.commit_target();return first,a.selected

    def select(self,key):
        self.app.steps.selection_set(key);self.app.select_step();self.root.update()

    def test_inspection_drafts_survive_step_switch_and_product_save(self):
        first,second=self.steps();p=self.app.episode_adjust_panel.inspection
        p.product.set('A');p.timeout.set('12');self.select(first)
        self.assertEqual(p.timeout.get(),'5');self.assertEqual(p.product.get(),'A');self.assertTrue(p.dirty)
        p.save_product();self.assertTrue(p.dirty);self.select(second);self.assertEqual(p.timeout.get(),'12')
        p.save_check();self.assertFalse(p.dirty);self.assertEqual(self.app.episode['steps'][1]['inspection']['timeout_seconds'],12)

    def test_each_step_draft_restores_its_station_and_target(self):
        first,second=self.steps();p=self.app.episode_adjust_panel.inspection
        p.station.set('운반용 지그');p.location_changed();p.target.set('중단 부품');self.select(first)
        p.timeout.set('9');self.select(second)
        self.assertEqual(p.station.get(),'운반용 지그');self.assertEqual(p.target.get(),'중단 부품');self.assertIn('1, 2',p.status.get())
        p.discard();self.select(first);self.assertEqual(p.timeout.get(),'9')

    def test_saving_one_check_keeps_other_step_pending(self):
        first,second=self.steps();p=self.app.episode_adjust_panel.inspection;p.product.set('A');p.timeout.set('9');self.select(first);p.save_check()
        self.assertTrue(self.app.episode_adjust_panel.dirty);self.assertIn('미저장',p.status.get());self.select(second);p.save_check();self.assertFalse(p.dirty)

    def test_inspection_and_execution_drafts_survive_episode_switch(self):
        self.steps();a=self.app;p=a.episode_adjust_panel;eid=a.episode['id'];p.values['speed'].set('350');p.inspection.product.set('B');p.inspection.timeout.set('8');key=a.selected
        a.new_episode();self.assertFalse(p.dirty);self.assertEqual(p.values['speed'].get(),'400')
        index=next(i for i,(_,d) in enumerate(a.library_entries) if d['id']==eid);a.library.selection_clear(0,'end');a.library.selection_set(index);a.load_selected_episode();self.select(key)
        self.assertEqual(p.values['speed'].get(),'350');self.assertEqual(p.inspection.product.get(),'B');self.assertEqual(p.inspection.timeout.get(),'8');self.assertTrue(p.dirty)

    def test_saved_execution_settings_do_not_leak_into_new_episode(self):
        self.steps();p=self.app.episode_adjust_panel;p.values['speed'].set('300');p.save_settings();self.app.new_episode();self.assertEqual(p.values['speed'].get(),'400')

    def test_discard_restores_saved_settings_without_changing_disk(self):
        self.steps();p=self.app.episode_adjust_panel;p.values['speed'].set('350');p.save_settings();saved=self.app.store.load(self.app.store.directory/(self.app.episode['id']+'.json'))
        p.values['speed'].set('300');p.discard();self.assertEqual(p.values['speed'].get(),'350');self.assertFalse(p._dirty)
        self.assertEqual(self.app.store.load(self.app.store.directory/(self.app.episode['id']+'.json')),saved)

    def test_unavailable_inspection_controls_explain_selection(self):
        p=self.app.episode_adjust_panel.inspection;self.assertTrue(p.station_choice.instate(['disabled']));self.assertTrue(p.remove_button.instate(['disabled']))
        self.steps();self.assertFalse(p.station_choice.instate(['disabled']));self.assertTrue(p.remove_button.instate(['disabled']))

    def test_invalid_numeric_input_is_actionable_and_preserved(self):
        self.steps();p=self.app.episode_adjust_panel;p.inspection.product.set('A');p.inspection.timeout.set('abc')
        with self.assertRaisesRegex(ValueError,'2~30초'):p.inspection.save_check()
        self.assertEqual(p.inspection.timeout.get(),'abc');self.assertTrue(p.dirty);p.values['timeout_seconds'].set('abc')
        with self.assertRaisesRegex(ValueError,'숫자'):p.save_settings()

    def test_teaching_guidance_matches_tab_after_page_return(self):
        a=self.app;a.open_episode_adjust();self.root.update();a.show_page('devices');a.show_page('teach');self.assertIn('Pi 에피소드 내보내기',a.guidance.get())

    def test_quick_help_opens_inspection_without_connection(self):
        from tkinter import ttk
        a=self.app;a.open_help();self.root.update()
        def walk(w):
            yield w
            for child in w.winfo_children():yield from walk(child)
        button=next(w for w in walk(a.settings.pages['help']) if isinstance(w,ttk.Button) and w.cget('text')=='3. 완제품·안착 검사')
        button.invoke();self.root.update();self.assertEqual(a.page,'teach');self.assertTrue(a.episode_adjusting());self.assertEqual(a.episode_adjust_panel.tabs.select(),str(a.episode_adjust_panel.inspection));self.assertIsNone(a.session)

    def test_draft_actions_fit_maximized_window(self):
        self.steps();a=self.app;self.root.geometry('1920x1080');a.open_episode_adjust();p=a.episode_adjust_panel
        for tab,button in ((0,p.reset_button),(1,p.inspection.reset_button)):
            p.tabs.select(tab);self.root.update();self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),p.tabs.winfo_rooty()+p.tabs.winfo_height())

    def test_late_pi_response_preserves_new_inspection_input(self):
        from concurrent.futures import Future
        self.steps();a=self.app;sync=a.episode_sync;sync.start_signature=sync.editor_signature();sync.job=Future()
        a.episode_adjust_panel.inspection.product.set('B');sync.job.set_result({});sync.poll()
        self.assertFalse(sync.ready);self.assertIn('현재 편집',a.pi_episode_status.get());self.assertEqual(a.episode_adjust_panel.inspection.product.get(),'B')
