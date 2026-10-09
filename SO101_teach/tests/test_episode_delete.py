import unittest
from unittest.mock import patch
from types import SimpleNamespace
from . import test_ui as fixtures
from so101_teach.domain import read_json

class EpisodeDeleteTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def saved(self,name):
        a=self.app;a.new_episode();a.episode_name.set(name);a.commit_target();return a.episode['id'],a.save_episode()
    def test_confirm_deletes_only_selected_and_last_delete_clears_state(self):
        a=self.app;one,p1=self.saved('첫째');two,p2=self.saved('둘째')
        a.request_episode_delete();self.assertTrue(p2.exists());a.confirm_episode_delete();self.root.update()
        self.assertFalse(p2.exists());self.assertTrue(p1.exists());self.assertEqual(a.episode['id'],one)
        self.assertEqual(read_json(self.data/'preferences.json')['last_episode_id'],one)
        a.request_episode_delete();a.confirm_episode_delete();self.root.update()
        self.assertFalse(p1.exists());self.assertEqual(a.episode['steps'],[]);self.assertNotIn(a.episode['id'],(one,two))
        self.assertIsNone(a.selected);self.assertEqual(a.library.size(),0);self.assertNotIn('last_episode_id',read_json(self.data/'preferences.json'))
        self.assertEqual(a.transport.targets,[]);self.assertIsNone(a.session)
    def test_cancel_and_changed_selection_never_delete_file(self):
        a=self.app;one,p1=self.saved('첫째');a.request_episode_delete();a.cancel_episode_delete();self.assertTrue(p1.exists())
        a.request_episode_delete();two,p2=self.saved('둘째')
        with self.assertRaisesRegex(ValueError,'바뀌'):a.confirm_episode_delete()
        self.assertTrue(p1.exists());self.assertTrue(p2.exists());self.assertEqual(a.episode['id'],two)
    def test_failed_unlink_keeps_selection_and_confirmation(self):
        a=self.app;one,path=self.saved('보존');a.request_episode_delete()
        with patch('pathlib.Path.unlink',side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):a.confirm_episode_delete()
        self.assertTrue(path.exists());self.assertEqual(a.episode['id'],one);self.assertEqual(a.episode_delete_target,one)
    def test_running_motion_blocks_both_delete_stages(self):
        a=self.app;one,path=self.saved('실행');a.request_episode_delete();a.safe_entry={'request_id':1}
        with self.assertRaises(ValueError):a.confirm_episode_delete()
        with self.assertRaises(ValueError):a.request_episode_delete()
        self.assertTrue(path.exists());self.assertEqual(a.episode['id'],one);a.safe_entry=None
    def test_store_rejects_path_escape(self):
        with self.assertRaises(ValueError):self.app.store.delete('../profile')
        self.assertTrue((self.data/'profile.json').exists())
