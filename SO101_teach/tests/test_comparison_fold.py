import unittest
from copy import deepcopy
from unittest.mock import patch
from tests import test_ui as fixtures
from so101_teach.domain import read_json

class ComparisonFoldTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def teach(self):
        self.fake_jig_camera();a=self.app;a.follow_jig.set(True);a.commit_target();self.root.update();return a
    def test_toggle_grows_preview_and_retains_values(self):
        a=self.teach();a.update_live_jig_comparison();p=a.jig_comparison;before=deepcopy(a.episode);revision=a.catalog.revision;values=[p.table.item(str(i),'values') for i in range(3)];height=a.preview_canvas.winfo_height()
        with patch.object(a,'motion_request',side_effect=AssertionError('No motor commands')):
            p.toggle_button.invoke();self.root.update()
            self.assertTrue(p.collapsed);self.assertFalse(p.table.winfo_ismapped());self.assertTrue(p.toggle_button.winfo_ismapped())
            self.assertGreater(a.preview_canvas.winfo_height(),height+60)
            p.toggle_button.invoke();self.root.update()
        self.assertTrue(p.table.winfo_ismapped());self.assertEqual([p.table.item(str(i),'values') for i in range(3)],values);self.assertEqual(a.episode,before);self.assertEqual(a.catalog.revision,revision)
    def test_refresh_and_empty_episode_do_not_reopen_collapsed_panel(self):
        a=self.teach();p=a.jig_comparison;p.toggle();saved=deepcopy(a.episode['steps']);a.episode['steps']=[];p.show([], '빈 목록');self.root.update()
        self.assertTrue(p.winfo_ismapped());self.assertTrue(p.toggle_button.winfo_ismapped());self.assertFalse(p.table.get_children());a.episode['steps']=saved;self.fake_jig_camera((243,-136,70));a.update_live_jig_comparison();self.root.update()
        self.assertTrue(p.winfo_ismapped());self.assertTrue(p.collapsed);self.assertFalse(p.table.winfo_ismapped())
        self.assertEqual(p.groups[0]['measured']['pose'],[243,-136,70]);p.toggle();self.root.update();self.assertTrue(p.table.winfo_ismapped())
    def test_fold_preference_survives_restart(self):
        a=self.teach();a.jig_comparison.toggle();self.assertTrue(read_json(self.data/'preferences.json')['jig_comparison_collapsed'])
        from so101_teach.ui import App
        import tkinter as tk
        a.camera.running=False;a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False);self.root.update()
        self.assertTrue(self.app.jig_comparison.collapsed);self.assertFalse(self.app.jig_comparison.table.winfo_ismapped())
