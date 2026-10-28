"""The actual archived protrusion must recover without weakening input evidence."""
from copy import deepcopy
import hashlib,json
import unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.mask_pose import detect_mask_seeded
from so101_teach import cad_pallet

class PalletShapeRoiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/pallet-shape-roi';cls.data=json.loads((cls.folder/'fixture.json').read_text())
        raw=(cls.folder/'last-received.jpg').read_bytes();assert hashlib.sha256(raw).hexdigest()==cls.data['image_sha256']
        cls.frame=cv2.imread(str(cls.folder/'last-received.jpg'));cls.o=cls.data['jig']
    def rows(self):
        with np.load(self.folder/'masks.npz',allow_pickle=False) as z:
            return [{'class_id':int(c),'confidence':float(s),'box':b.tolist(),'mask':mask} for c,s,b,mask in zip(z['classes'],z['scores'],z['boxes'],z['masks'])]
    def call(self,rows,roi=None):
        o=self.o;return detect_mask_seeded(self.frame,o['mesh'],o['roi'] if roi is None else roi,o['profile'],rows,self.data['settings'])
    def test_archived_spur_recovers_with_original_contour_support(self):
        rows=self.rows();target=next(r for r in rows if r['class_id']==1);before=target['mask'].copy();o=self.o
        original=cad_pallet.detect(self.frame,o['mesh'],o['roi'],o['profile'],before)
        self.assertIsNone(original['selected']);self.assertGreater(original['cad_fit']['rms_px'],5.9)
        result=self.call(rows);self.assertIsNotNone(result['selected']);proof=result['cad_fit']['cad_shape_roi']
        self.assertLess(result['cad_fit']['rms_px'],1.3);self.assertLessEqual(proof['removed_fraction'],.03)
        self.assertGreaterEqual(proof['original_contour_inlier_fraction'],.8)
        self.assertLessEqual(max(proof['seed_variants_pose_difference_mm_deg']),.2)
        self.assertFalse(result['verified_for_motion']);np.testing.assert_array_equal(target['mask'],before)
    def test_original_user_roi_cannot_be_replaced_by_cad_roi(self):
        result=self.call(self.rows(),roi=[0,0,.1,.1]);self.assertIsNone(result['selected']);self.assertEqual(result['cad_pallet_reason'],'roi')
    def test_multiple_current_instances_disable_shape_recovery(self):
        rows=self.rows();target=next(r for r in rows if r['class_id']==1)
        result=self.call(rows+[deepcopy(target)]);self.assertIsNone(result['selected'])
    def test_no_current_mask_never_invents_cad_observation(self):
        result=self.call([r for r in self.rows() if r['class_id']!=1]);self.assertIsNone(result['selected']);self.assertEqual(result['status'],'model_not_found')
    def test_large_nonphysical_border_remains_rejected(self):
        rows=self.rows();target=next(r for r in rows if r['class_id']==1)
        target['mask']=cv2.dilate(target['mask'].astype(np.uint8),np.ones((15,15),np.uint8))>0
        self.assertIsNone(self.call(rows)['selected'])
