import json,unittest
from pathlib import Path
import cv2,numpy as np
from so101_teach.devices import CameraSession
from so101_teach.height_reference import support_plane_profile
from so101_teach.vision import detect,StableCandidate

class StationaryDetectionTests(unittest.TestCase):
    folder=Path(__file__).parent/'fixtures/vision-stationary'
    def values(self,key):
        config=json.loads((self.folder/'fixture.json').read_text());j=config['jigs'][key]
        return {**j['mesh'],'shape':j['shape'],'method':j['method']},j['roi'],support_plane_profile(config['profile'],j)
    def test_faint_opening_seed_keeps_original_outer_and_hole_checks(self):
        m,r,p=self.values('pallet')
        for name in ('pallet-gap.png','pallet-fragment.png'):
            with self.subTest(frame=name):
                out=detect(cv2.imread(str(self.folder/name)),m,r,p);c=out['selected']
                self.assertIsNotNone(c);np.testing.assert_allclose(c['center_px'],[431,189],atol=2)
                self.assertGreaterEqual(min(c['edge_support'][:4]),.25)
                self.assertGreaterEqual(sorted(c['edge_support'][4:])[1],.55)
                self.assertFalse(out['verified_for_motion'])
    def test_stationary_loaded_carrier_does_not_alternate_between_inner_and_outer_rims(self):
        m,r,p=self.values('b5ebdb54807c441c8267c15d77713b6a')
        results=[detect(cv2.imread(str(self.folder/name)),m,r,p) for name in ('carrier-a.png','carrier-b.png')]
        for out in results:self.assertTrue(out['selected']['model_recovered'])
        centers=np.array([out['selected']['center_px'] for out in results]);self.assertLess(np.linalg.norm(centers[1]-centers[0]),4)
        stable=StableCandidate(3)
        self.assertIsNone(stable.update(results[0],0)['selected'])
        for t in (.7,1.4,2.1,2.8):self.assertIsNone(stable.update(results[1],t)['selected'])
        self.assertIsNotNone(stable.update(results[1],3.1)['selected'])
    def test_slow_result_is_age_labelled_without_refreshing_measurement(self):
        c=CameraSession(0);frame=np.zeros((20,30,3),np.uint8);raw={'selected':{'id':'pallet'},'candidates':[],'status':'shape_match'}
        detection={'selected':raw['selected'],'candidates':[],'live_by_jig':{'pallet':raw}}
        c.observation=(frame,detection,10.);c.preview_frame=(frame,10.8)
        data=c.preview_observation[1]
        self.assertEqual(data['live_by_jig']['pallet']['selected'],raw['selected'])
        self.assertAlmostEqual(data['live_by_jig']['pallet']['preview_detection_age_s'],.8)
        self.assertEqual(data['detection_frame_at'],10.);self.assertNotIn('preview_detection_age_s',raw)
        # A real negative result must clear the outline immediately, even while
        # the old accepted position is still held elsewhere for teaching.
        missing={'selected':None,'candidates':[],'status':'not_found'}
        c.observation=(frame,{'live_by_jig':{'pallet':missing}},10.1)
        self.assertIsNone(c.preview_observation[1]['live_by_jig']['pallet']['selected'])
        c.preview_frame=(frame,12.)
        self.assertEqual(c.preview_observation[1]['live_by_jig'],{})
