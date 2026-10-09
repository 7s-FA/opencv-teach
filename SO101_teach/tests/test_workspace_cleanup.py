import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from copy import deepcopy
from . import test_ui as fixtures
from so101_teach.domain import read_json

class WorkspaceCleanupTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def widgets(self,w):
        result=[]
        for child in w.winfo_children():result.append(child);result.extend(self.widgets(child))
        return result

    def test_controls_removed_monitor_named_and_step_list_has_room(self):
        a=self.app;self.root.geometry('1180x760');self.root.update()
        texts=[str(w.cget('text')) for w in self.widgets(self.root) if w.winfo_class()=='TButton']
        for label in ('↑','↓','미리보기 정지','선택 지그의 티칭 기준 갱신'):self.assertNotIn(label,texts)
        self.assertIn('장비 모니터링',a.nav_buttons['devices'].cget('text'));a.show_page('devices');self.assertEqual(a.page_title.get(),'장비 모니터링')
        a.show_page('teach');self.root.update();self.assertGreaterEqual(a.steps.winfo_height(),100)
        self.assertEqual(a.delete_step_btn.winfo_rooty(),a.safe_edit_btn.winfo_rooty())
        self.assertGreater(a.delete_step_btn.winfo_rooty(),a.steps.winfo_rooty()+a.steps.winfo_height())
        others=[w for w in a.teach_reread_jig_btn.master.winfo_children() if w!=a.teach_reread_jig_btn]
        self.assertTrue(all(w.winfo_rootx()+w.winfo_width()<=a.teach_reread_jig_btn.winfo_rootx() for w in others))

    def test_drag_reorder_and_lower_delete_still_work(self):
        a=self.app
        for name in ('A','B','C'):a.step_name.set(name);a.commit_target()
        original=[s['id'] for s in a.episode['steps']];a.drag_step=original[0]
        with patch.object(a.steps,'identify_row',return_value=original[2]):a.drop_step(SimpleNamespace(y=1))
        self.assertEqual([s['name'] for s in a.episode['steps']],['B','C','A'])
        a.delete_step_btn.invoke();self.assertEqual([s['name'] for s in a.episode['steps']],['B','C'])

    def test_camera_hold_saves_without_committing_model_drafts_and_applies_to_new_jigs(self):
        a=self.app;a.save_model();before=read_json(a.adjustments_path)
        a.trim_vars['shoulder_lift'].set(23);a.table_z.set('44');a.hold_seconds.set('7');a.save_camera_hold()
        saved=read_json(a.adjustments_path);self.assertEqual(saved['trim_ticks'],before['trim_ticks']);self.assertEqual(saved['table_z_mm'],before['table_z_mm']);self.assertEqual(saved['hold_seconds'],7)
        self.assertEqual(a.pose_latch.seconds,7);self.assertTrue(str(a.hold_seconds_spin).startswith(str(a.pages['camera'])))
        other=a.catalog.duplicate('pallet');a.detector.refresh();self.assertEqual(a.detector.latches[other['id']].seconds,7)
        for bad in ('nan','-1','61'):
            a.hold_seconds.set(bad)
            with self.assertRaises(ValueError):a.save_camera_hold()
        self.assertEqual(read_json(a.adjustments_path),saved)

    def test_frozen_hold_change_does_not_modify_settings_or_teaching(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True);a.commit_target();before=deepcopy(a.episode)
        a.set_pose_frozen(True);a.hold_seconds.set('3')
        with self.assertRaises(ValueError):a.save_camera_hold()
        self.assertEqual(a.episode,before);self.assertFalse(a.adjustments_path.exists());a.set_pose_frozen(False)

    def test_image_save_shows_full_path_and_folder_button_uses_that_folder(self):
        a=self.app;self.fake_jig_camera();path=a.save_camera()
        self.assertTrue(path.exists());self.assertTrue((path.parent/'metadata'/path.with_suffix('.json').name).exists())
        self.assertEqual(a.camera_save_path.get(),str(path.resolve()));self.assertIn(str(path.resolve()),a.message.get())
        with patch('subprocess.Popen') as open_folder:a.open_capture_folder();self.assertEqual(open_folder.call_args.args[0],['xdg-open',str(path.parent.resolve())])

    def test_pause_and_continue_keep_preview_position_without_stop_button(self):
        a=self.app;a.commit_target();a.play();a.pause_preview();self.assertTrue(a.transport.paused);self.assertEqual(a.pause_btn.cget('text'),'이어보기')
        elapsed=a.transport.elapsed;a.pause_preview();self.assertFalse(a.transport.paused);self.assertEqual(a.transport.elapsed,elapsed);self.assertEqual(a.pause_btn.cget('text'),'일시정지')

    def test_photo_scrollbars_and_wheel_scroll_list_not_outer_settings(self):
        a=self.app;s=a.settings;a.show_page('settings');s.tabs.select(s.pages['camera']);s.photo_paths=[Path(f'photo-{i:03}.jpg') for i in range(60)];s.show_photos();self.root.update()
        outer=s.scroll_canvases[str(s.pages['camera'])];outer.yview_moveto(.15);self.root.update();old=outer.yview()
        s.photos.event_generate('<Button-5>');self.root.update();self.assertGreater(s.photos.yview()[0],0);self.assertEqual(outer.yview(),old)
        self.root.tk.call(s.photo_scroll_y.cget('command'),'moveto',.8);self.assertGreater(s.photos.yview()[0],.5)
        s.photos.column('file',width=1500,stretch=False);self.root.update();self.root.tk.call(s.photo_scroll_x.cget('command'),'moveto',.5);self.assertGreater(s.photos.xview()[0],0)

    def test_in_app_photo_folder_browser_has_scrollbars(self):
        a=self.app;s=a.settings;folder=self.data/'photos';folder.mkdir()
        for i in range(50):(folder/f'album-{i:03}').mkdir()
        s.vars['photo_dir'].set(str(folder));a.show_page('settings');s.choose('photo_dir',directory=True);self.root.update()
        page=self.root.nametowidget(s.tabs.select());widgets=self.widgets(page);listing=next(w for w in widgets if w.winfo_class()=='Listbox')
        bars=[w for w in widgets if w.winfo_class()=='TScrollbar'];self.assertEqual(len(bars),2)
        listing.event_generate('<Button-5>');self.root.update();self.assertGreater(listing.yview()[0],0)
