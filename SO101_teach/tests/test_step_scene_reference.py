import unittest
from copy import deepcopy
from unittest.mock import patch
from tests.test_taught_scene import TaughtSceneTests

class StepSceneReferenceTests(unittest.TestCase):
    setUp=TaughtSceneTests.setUp
    tearDown=TaughtSceneTests.tearDown
    fake_jig_camera=TaughtSceneTests.fake_jig_camera
    teach=TaughtSceneTests.teach
    def render_request(self,a):
        a.last_render=None
        return TaughtSceneTests.render_request(self,a)
    def execute(self,a,action='play'):
        current={'pallet':deepcopy(a.current_jig_reference())}
        plan=[{'ticks':a.target.copy(),'corrected':False}]
        with patch('so101_teach.geometry.corrected_plan',return_value=plan),patch.object(a,'motion_request',return_value=10):
            a.begin_execution(action,deepcopy(a.episode['steps']),current)
        return current
    def test_idle_target_uses_saved_jig_even_with_different_live_detection(self):
        a=self.teach();before=deepcopy(a.episode)
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        self.assertEqual(a.current_jig_reference()['pose'],[250,-90,12]);self.assertEqual(a.episode,before)
    def test_idle_target_without_camera_uses_saved_reference(self):
        a=self.teach();a.camera=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        self.assertIn('티칭 저장 기준',a.preview_caption.get())
    def test_second_editor_uses_its_own_saved_reference(self):
        a=self.teach();first=a.selected;other=deepcopy(a.episode['steps'][0]);other['id']='second';other['jig_reference']['pose']=[100,200,30];a.episode['steps'].append(other);a.refresh_steps();self.root.update()
        from so101_teach.step_editor import SecondEditor
        a.second_editor=SecondEditor(a);a.second_editor.load('second')
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[100,200,30]);self.assertEqual(a.selected,first)
        a.close_second_editor();self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
    def test_new_unsaved_following_jig_does_not_pretend_live_pose_is_saved(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True)
        self.assertEqual(a.saved_scene_references(),{});self.assertIsNone(self.render_request(a)['jigs'][0]['pose'])
        self.assertIn('저장 기준 없음',a.preview_caption.get())
        a.follow_jig.set(False);self.assertIsNone(a.saved_scene_references())
    def test_preview_pause_seek_and_completion_preserve_computation_reference(self):
        a=self.teach();current=self.execute(a,'preview');self.fake_jig_camera((300,100,45))
        a.pause_preview();a.seek_preview(1)
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],current['pallet']['pose'])
        a.stop_preview(quiet=True);a.camera=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],current['pallet']['pose'])
        a.apply_target(a.target);self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
    def test_episode_execution_uses_snapshot_after_camera_closes(self):
        a=self.teach();current=self.execute(a);a.camera=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],current['pallet']['pose'])
        self.assertIn('실행 측정 기준',a.preview_caption.get());current['pallet']['pose'][0]=999
        self.assertEqual(a.measured_scene_references()['pallet']['pose'],[250,-90,12])
    def test_new_step_edit_returns_saved_scene_but_live_view_keeps_execution_scene(self):
        a=self.teach();self.execute(a,'move');a.apply_target(a.target)
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        a.mode='live';self.assertIsNone(a.saved_scene_references())
        self.assertEqual(a.measured_scene_references()['pallet']['pose'],[250,-90,12])
    def test_failed_execution_does_not_install_measurement_scene(self):
        a=self.teach();plan=[{'ticks':a.target.copy(),'corrected':False}]
        with patch('so101_teach.geometry.corrected_plan',return_value=plan),self.assertRaises(ValueError):a.begin_execution('move',a.episode['steps'],{'pallet':a.current_jig_reference()})
        self.assertIsNone(a.measured_jig_display);self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
    def test_taught_execution_overrides_previous_measured_scene(self):
        a=self.teach();self.execute(a)
        with patch.object(a,'motion_request',return_value=11):a.execute_taught_episode()
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
    def test_multiple_measured_jigs_are_copied_independently(self):
        a=self.teach();current=self.execute(a);current['other']=deepcopy(current['pallet']);current['other']['pose']=[50,60,70]
        a.measured_jig_display=deepcopy(current);refs=a.measured_scene_references();refs['other']['pose'][0]=999
        self.assertEqual(a.measured_scene_references()['other']['pose'],[50,60,70]);self.assertEqual(a.measured_scene_references()['pallet']['pose'],[250,-90,12])

    def test_step_selection_during_execution_can_show_saved_target_separately(self):
        import threading
        from types import SimpleNamespace
        a=self.teach();self.execute(a);a.apply_target(a.target)
        active=threading.Event();active.set();a.session=SimpleNamespace(program_active=active)
        try:self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])
        finally:a.session=None
    def test_measurement_pending_cancel_returns_to_saved_reference(self):
        a=self.teach();a.pending_execution={'page':'teach'}
        self.assertIsNone(a.saved_scene_references());a.stop_preview(quiet=True)
        self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])
    def test_live_adjust_uses_current_scene_and_off_returns_to_saved_editing(self):
        a=self.teach();a.live_adjust.owner=a
        self.assertIsNone(a.saved_scene_references());a.live_adjust.owner=None
        self.assertEqual(a.saved_scene_references()['pallet']['pose'],[228,-138,67])
    def test_second_jig_choice_cannot_reuse_the_previous_jigs_saved_pose(self):
        a=self.teach();other=deepcopy(a.catalog.items['pallet']);other.update(id='other',name='다른 지그');a.catalog.items['other']=other
        a.step_jig_choice.configure(values=[v['name'] for v in a.catalog.items.values()]);a.step_jig_choice.current(1)
        self.assertEqual(set(a.saved_scene_references()),{'pallet'})
