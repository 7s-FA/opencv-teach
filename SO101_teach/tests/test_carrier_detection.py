import unittest,json
from copy import deepcopy
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import detect,supported_outer_quad

class CarrierDetectionTests(unittest.TestCase):
    def setUp(self):
        self.folder=ROOT/'verification/carrier-detection'
        self.fixture=json.loads((self.folder/'fixture.json').read_text())
    def detect(self,frame,**changes):
        cfg={**self.fixture,**changes}
        return detect(frame,cfg['mesh'],cfg['roi'],cfg['profile'])
    def test_live_frames_recover_complete_carrier_not_individual_insert(self):
        for name in ('before','sample2','sample3','sample4','sample5','sample6','sample7'):
            with self.subTest(name=name):
                frame=cv2.imread(str(self.folder/(name+'.jpg')));out=self.detect(frame)
                self.assertIsNotNone(out['selected']);self.assertTrue(out['lens_corrected'])
                p=out['selected'];np.testing.assert_allclose(p['center_px'],[672,418],atol=10)  # Plane-projected center stays inside the photographed central crossing.
                self.assertGreater(p['area_px'],90000)
                self.assertLess(p['outer_shape']['size_error_ratio'],.06)
                self.assertEqual(p['symmetry_deg'],180)
    def test_recovery_still_rejects_cut_roi_wrong_dimensions_and_empty_scene(self):
        frame=cv2.imread(str(self.folder/'sample2.jpg'))
        self.assertIsNone(self.detect(frame,roi=[.45,.35,.61,.8])['selected'])
        wrong={**self.fixture['mesh'],'size_mm':[220,220,6]}
        self.assertIsNone(self.detect(frame,mesh=wrong)['selected'])
        self.assertIsNone(self.detect(np.full_like(frame,180))['selected'])
    def test_hull_requires_evidence_for_each_side(self):
        contour=np.int32([[[20,20]],[[180,20]],[[180,180]],[[20,180]]])
        edges=np.zeros((200,200),np.uint8)
        for a,b in (((20,20),(180,20)),((180,20),(180,180)),((180,180),(20,180))):cv2.line(edges,a,b,255,1)
        self.assertIsNone(supported_outer_quad(contour,edges))
        cv2.line(edges,(20,180),(20,20),255,1)
        self.assertIsNotNone(supported_outer_quad(contour,edges))
