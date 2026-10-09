import unittest
from copy import deepcopy
from unittest.mock import patch
from . import test_ui as fixtures

class FirstFollowSaveTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def test_checkbox_and_poll_do_not_capture_save_uses_latest_confirmed_pose(self):
        a=self.app;self.fake_jig_camera((100,-50,10));a.follow_jig.set(True);a.update_jig_hint()
        self.root.update();a.ensure_teaching_reference()
        self.assertEqual(a.episode['jig_references'],{});self.assertIn('새 스텝 기준',a.jig_hint.get())
        self.fake_jig_camera((250,-110,85));a.commit_target()
        self.assertEqual(a.episode['jig_references']['pallet']['pose'],[250,-110,85])
        self.fake_jig_camera((270,-90,80));a.commit_target()
        self.assertTrue(all(s['jig_reference']['pose']==[250,-110,85] for s in a.episode['steps']))

    def test_unsaved_step_execution_cannot_set_episode_basis(self):
        a=self.app;self.fake_jig_camera((100,-50,10));a.follow_jig.set(True)
        with patch.object(a,'prepare_execution') as execute:a.execute_target();execute.assert_called_once()
        self.assertEqual(a.episode['jig_references'],{});self.assertEqual(a.episode['steps'],[])
        self.fake_jig_camera((250,-110,85));a.commit_target();self.assertEqual(a.episode['jig_references']['pallet']['pose'],[250,-110,85])

    def test_first_saved_basis_survives_reorder_and_removing_first_step(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target();first=a.selected
        self.fake_jig_camera((250,-110,85));a.slider_tick('gripper',str(a.target['gripper']+1));a.commit_target();second=a.selected;a.move_step(-1)
        a.selected=first;a.delete_step();self.assertEqual(a.episode['steps'][0]['id'],second)
        a.follow_jig.set(True);a.commit_target();self.assertTrue(all(s['jig_reference']['pose']==[228,-138,67] for s in a.episode['steps']))

    def test_first_saved_pose_is_independent_for_each_jig(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target()
        item=a.catalog.duplicate('pallet');key=item['id'];a.catalog_changed();a.set_step_jig(key)
        self.fake_jig_camera((250,-110,85));frame,result,at=a.camera.observation;result['by_jig']={'pallet':deepcopy(result),key:deepcopy(result)}
        a.update_jig_hint();self.assertNotIn(key,a.episode['jig_references']);a.commit_target()
        self.assertEqual(a.episode['jig_references']['pallet']['pose'],[228,-138,67]);self.assertEqual(a.episode['jig_references'][key]['pose'],[250,-110,85])

    def test_new_episode_does_not_share_previous_first_save(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target();old=deepcopy(a.episode)
        a.new_episode();self.fake_jig_camera((250,-110,85));a.follow_jig.set(True);a.commit_target()
        self.assertNotEqual(a.episode['id'],old['id']);self.assertEqual(a.episode['jig_references']['pallet']['pose'],[250,-110,85])
        self.assertEqual(old['jig_references']['pallet']['pose'],[228,-138,67])

    def test_failed_first_save_without_detection_leaves_no_basis(self):
        a=self.app;a.follow_jig.set(True);a.update_jig_hint();before=deepcopy(a.episode)
        with self.assertRaises(ValueError):a.commit_target()
        self.assertEqual(a.episode,before)
