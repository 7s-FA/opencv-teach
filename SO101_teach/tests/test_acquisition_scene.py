import unittest,time
from copy import deepcopy
from so101_teach.camera_lifecycle import MEASUREMENT_MAX_AGE_SECONDS
from unittest.mock import patch
from tests import test_taught_scene as fixtures

class AcquisitionSceneTests(unittest.TestCase):
    setUp=fixtures.TaughtSceneTests.setUp
    tearDown=fixtures.TaughtSceneTests.tearDown
    fake_jig_camera=fixtures.TaughtSceneTests.fake_jig_camera
    teach=fixtures.TaughtSceneTests.teach
    render_request=fixtures.TaughtSceneTests.render_request
    def test_simulation_uses_unexpired_adoption_when_capture_is_over_one_second_old(self):
        a=self.app;a.page='model';self.fake_jig_camera((180,-130,209.4));a.camera_sleeping=False
        frame,result,at=a.camera.observation;result['selected']['metric']['symmetry_deg']=360;result['selected']['symmetry_deg']=360
        a.camera.observation=(frame,result,time.monotonic()-(MEASUREMENT_MAX_AGE_SECONDS+.2))
        self.assertEqual(a.camera_results(),{})
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[180,-130,209.4]);self.assertIn('현재 채택 기준',a.preview_caption.get())
        self.assertEqual(a.camera_results(),{});self.assertEqual(result['selected']['metric']['yaw_deg'],209.4)
    def test_new_adoption_is_displayed_exactly_without_transitional_position_or_heading(self):
        a=self.app;a.page='model';self.fake_jig_camera((180,-130,359.))
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[180,-130,359.])
        self.fake_jig_camera((185,-120,1.));a.last_render=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[185,-120,1.])
    def test_saved_step_scene_still_uses_its_saved_basis_and_does_not_mutate_episode(self):
        a=self.teach();before=deepcopy(a.episode)
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        a.toggle_jig_updates();a.last_render=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67]);self.assertEqual(a.episode,before)
        a.page='model';a.last_render=None
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[250,-90,12])
