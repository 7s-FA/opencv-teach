from copy import deepcopy
from pathlib import Path
import json,unittest
from unittest.mock import Mock,patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.model_regions import checked_settings,search_regions,identity
from so101_teach.lens_geometry import DetectionFrame,detection_region
from so101_teach.vision import detect

SETTINGS={'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}

class ModelRegionTests(unittest.TestCase):
 def test_provider_code_changes_invalidate_the_saved_model_identity(self):
  first={'files':{'model.ncnn.bin':'a'*64},'_provider_sha256':{'yolo_worker.py':'b'*64}}
  changed={**first,'_provider_sha256':{'yolo_worker.py':'c'*64}}
  self.assertNotEqual(identity(first),identity(changed))
  self.assertNotEqual(identity(first),identity({'files':first['files']}))
 def test_settings_are_explicit_and_model_identity_is_required(self):
  self.assertEqual(checked_settings({})['method'],'opencv')
  self.assertEqual(checked_settings({})['confidence'],.5)
  self.assertEqual(checked_settings({'jig_detection':SETTINGS}),SETTINGS)
  self.assertEqual(checked_settings({'jig_detection':{**SETTINGS,'confidence':.75}})['confidence'],.75)
  for bad in ({**SETTINGS,'model_identity':None},{**SETTINGS,'confidence':.49},{**SETTINGS,'confidence':.755},{**SETTINGS,'confidence':float('nan')},{**SETTINGS,'model':'sam3'}):
   with self.assertRaises(ValueError):checked_settings({'jig_detection':bad})
 def test_regions_use_class_and_margin_not_segmentation_center(self):
  frame=np.zeros((100,200,3),np.uint8)
  boxes=[{'class_id':1,'confidence':.9,'box':[40,30,80,60]}, {'class_id':0,'confidence':.99,'box':[120,40,170,80]}]
  mask,regions=search_regions(frame,{'shape':'ring'},boxes,SETTINGS)
  self.assertEqual(regions[0]['box'],[24,14,96,76]);self.assertEqual(len(regions),1)
  self.assertEqual(mask[40,150],0);self.assertEqual(mask[15,25],255)
  self.assertNotIn('center',regions[0]);self.assertNotIn('angle',regions[0])
 def test_model_guided_rim_preserves_calibrated_pose_and_manual_roi(self):
  from tests.test_carrier_direction import CarrierDirectionTests
  helper=CarrierDirectionTests();helper.setUpClass();frame,profile=helper.synthetic(25,loaded=True)
  baseline=detect(frame,helper.mesh,profile=profile)['selected'];self.assertIsNotNone(baseline)
  quad=np.asarray(baseline['quad']);lo=quad.min(0);hi=quad.max(0)
  # Deliberately shift the model box centre; its centre must never become the pose.
  rows=[{'class_id':0,'confidence':.95,'box':[lo[0]-12,lo[1]-5,hi[0]+20,hi[1]+14]}]
  mask,_=search_regions(frame,helper.mesh,rows,SETTINGS)
  result=detect(frame,helper.mesh,profile=profile,search_mask=mask)['selected'];self.assertIsNotNone(result)
  np.testing.assert_allclose(result['metric']['center_xy_mm'],baseline['metric']['center_xy_mm'],atol=.3)
  self.assertAlmostEqual(result['metric']['yaw_deg'],baseline['metric']['yaw_deg'],delta=.2)
  blocked=detect(frame,helper.mesh,[0,0,.1,.1],profile,search_mask=mask)
  self.assertIsNone(blocked['selected'])
 def test_raw_search_mask_is_rectified_and_cropped_with_same_geometry(self):
  from tests.test_carrier_direction import CarrierDirectionTests
  helper=CarrierDirectionTests();helper.setUpClass();frame,profile=helper.synthetic(25)
  profile=deepcopy(profile);profile['intrinsics']['D']=[.04,-.01,0.,0.,0.]
  prepared=DetectionFrame(frame,profile);raw=np.zeros(frame.shape[:2],np.uint8);raw[100:400,200:600]=255
  expected=cv2.bitwise_and(cv2.remap(raw,*prepared.maps,cv2.INTER_NEAREST),prepared.valid)
  np.testing.assert_array_equal(prepared.search_mask(raw),expected)
  cropped=detection_region(prepared,[.3,.3,.6,.6]);x,y=cropped.offset
  np.testing.assert_array_equal(cropped.search_mask(raw),expected[y:y+cropped.height,x:x+cropped.width])
 def test_missing_model_region_clears_old_adoption_without_full_frame_fallback(self):
  from so101_teach.vision_service import MultiDetector
  from types import SimpleNamespace
  catalog=SimpleNamespace(items={'pallet':{'roi':None}},revision=0,mesh=lambda key:{'shape':'ring'})
  detector=MultiDetector(catalog,{'jig_detection':SETTINGS});detector.region_model=Mock();detector.region_model.predict.return_value=[]
  detector.refresh();latch=detector.latches['pallet'];latch.clear=Mock(wraps=latch.clear)
  with patch('so101_teach.vision_service.detect') as cvdetect:
   result=detector.process(np.zeros((50,60,3),np.uint8))
  cvdetect.assert_not_called();latch.clear.assert_called_once();self.assertIsNone(result['selected'])
  self.assertEqual(result['live_by_jig']['pallet']['status'],'model_not_found')
