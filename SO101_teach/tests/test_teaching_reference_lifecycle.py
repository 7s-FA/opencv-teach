import unittest
from copy import deepcopy
from . import test_ui as fixtures

class TeachingReferenceLifecycleTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def teach(self,pose=(228,-138,67)):
        a=self.app;self.fake_jig_camera(pose);a.follow_jig.set(True);a.commit_target();return a

    def test_idle_camera_does_not_latch_before_follow_is_enabled(self):
        a=self.app;self.fake_jig_camera((100,-50,10));self.assertIsNone(a.ensure_teaching_reference());self.assertEqual(a.episode['jig_references'],{})
        self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target()
        self.assertEqual(a.episode['steps'][0]['jig_reference']['pose'],[228,-138,67])

    def test_delete_last_follow_step_then_reteach_uses_new_pose_once(self):
        a=self.teach();a.delete_step();self.assertEqual(a.episode['jig_references'],{})
        self.fake_jig_camera((100,-50,10));self.assertIsNone(a.ensure_teaching_reference())
        self.fake_jig_camera((250,-110,85));a.follow_jig.set(True);a.commit_target()
        self.fake_jig_camera((270,-90,80));a.commit_target()
        self.assertTrue(all(s['jig_reference']['pose']==[250,-110,85] for s in a.episode['steps']))

    def test_delete_one_step_keeps_other_step_and_reference(self):
        a=self.teach();a.slider_tick('gripper',str(a.target['gripper']+1));a.commit_target();first=deepcopy(a.episode['steps'][0]);reference=deepcopy(a.episode['jig_references'])
        a.delete_step();self.assertEqual(a.episode['steps'],[first]);self.assertEqual(a.episode['jig_references'],reference)

    def test_deleting_one_jig_does_not_reset_another_jig(self):
        a=self.teach();first=a.selected;other=a.catalog.duplicate('pallet');a.catalog_changed();key=other['id']
        step=deepcopy(a.episode['steps'][0]);step.update(id='other-step',jig_id=key);step['jig_reference']['pose']=[50,60,70]
        a.episode['steps'].append(step);a.episode['jig_references'][key]=deepcopy(step['jig_reference']);a.selected=first;a.delete_step()
        self.assertNotIn('pallet',a.episode['jig_references']);self.assertEqual(a.episode['jig_references'][key]['pose'],[50,60,70])

    def test_disabling_last_follow_step_discards_unused_common_reference(self):
        a=self.teach();a.follow_jig.set(False);a.update_selected_step();self.assertEqual(a.episode['jig_references'],{})
        self.fake_jig_camera((250,-110,85));a.follow_jig.set(True);a.update_selected_step()
        self.assertEqual(a.episode['steps'][0]['jig_reference']['pose'],[250,-110,85])

    def test_reload_orphan_reference_is_not_reused_or_saved_over_original(self):
        a=self.app;self.fake_jig_camera((100,-50,10));a.episode['jig_references']['pallet']=a.current_jig_reference();a.commit_target();a.episode['jig_references']['pallet']=a.current_jig_reference();path=a.save_episode();saved=path.read_bytes()
        a.refresh_library();a.library.selection_clear(0,'end');a.library.selection_set(0);a.load_selected_episode()
        self.assertEqual(a.episode['jig_references'],{});self.assertEqual(path.read_bytes(),saved)
        self.fake_jig_camera((250,-110,85));a.follow_jig.set(True);a.commit_target();self.assertEqual(a.episode['steps'][-1]['jig_reference']['pose'],[250,-110,85])
