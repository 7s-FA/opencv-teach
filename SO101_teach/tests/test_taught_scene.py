import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from . import test_ui as fixtures
from so101_teach.jig_comparison import teaching_scene_references

class TaughtSceneTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def teach(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.follow_jig.set(True);a.commit_target()
        self.fake_jig_camera((250,-90,12))
        return a

    def start(self,a):
        with patch.object(a,'motion_request',return_value=17):a.execute_taught_episode()

    def render_request(self,a):
        renderer=Mock();renderer.submit.return_value=True;renderer.poll.return_value=None;a.renderer=renderer;a.render_enabled=True
        self.root.after_cancel(a.render_job);a.render_tick()
        return renderer.submit.call_args.args[2]

    def test_raw_execution_renders_saved_pose_not_camera_pose(self):
        a=self.teach();before=deepcopy(a.episode);self.start(a)
        scene=self.render_request(a)
        self.assertEqual(scene['jigs'][0]['pose'],[228,-138,67])
        self.assertIn('지그: 티칭 저장 기준',a.preview_caption.get());self.assertEqual(a.episode,before)
        self.assertEqual(a.current_jig_reference()['pose'],[250,-90,12])

    def test_saved_jig_is_visible_without_camera_and_survives_completion(self):
        a=self.teach();a.camera=None;self.start(a)
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        a.session=SimpleNamespace(active_request_id=17,index=1)
        try:self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])
        finally:a.session=None

    def test_different_step_references_follow_the_active_step_and_keep_other_jigs(self):
        a=self.teach();first=deepcopy(a.episode['steps'][0]);second=deepcopy(first);second['jig_reference']['pose']=[240,-100,30]
        other=deepcopy(first);other['jig_id']='other';other['jig_reference']['pose']=[50,60,70]
        steps=[{'ticks':first['ticks']},first,second,other];before=deepcopy(steps)
        a.taught_jig_display={'steps':steps,'request_id':17};a.session=SimpleNamespace(active_request_id=16,index=3)
        try:
            self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])
            a.session.active_request_id=17;a.session.index=2
            self.assertEqual(a.saved_scene_references()['pallet']['pose'],[240,-100,30])
            a.session.index=4;self.assertEqual(a.saved_scene_references()['other']['pose'],[50,60,70])
            refs=a.saved_scene_references();refs['pallet']['pose'][0]=0;self.assertEqual(steps,before)
        finally:a.session=None

    def test_normal_preview_clears_saved_scene_and_uses_new_measurement(self):
        a=self.teach();self.start(a)
        a.play();self.assertIsNone(a.taught_jig_display);self.assertIsNotNone(a.pending_execution)
        self.fake_jig_camera((230,-137,68));a.check_pending_execution();a.stop_preview()
        a.show_page('teach');scene=self.render_request(a)
        self.assertEqual(scene['jigs'][0]['pose'],[230,-137,68])
        self.assertNotIn('티칭 저장 기준',a.preview_caption.get())

    def test_fixed_only_raw_run_does_not_show_unrelated_camera_jig(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(False);a.commit_target();self.start(a)
        self.assertEqual(a.saved_scene_references(),{})
        self.assertIsNone(self.render_request(a)['jigs'][0]['pose'])

    def test_failed_dispatch_does_not_switch_to_saved_jig_scene(self):
        a=self.teach()
        with self.assertRaises(ValueError):a.execute_taught_episode()
        self.assertIsNone(a.taught_jig_display)
