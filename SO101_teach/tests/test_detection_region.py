import json,unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.vision import detect
from so101_teach.lens_geometry import DetectionFrame,detection_region

class DetectionRegionTests(unittest.TestCase):
    def test_cropped_detection_preserves_raw_pixels_world_pose_and_profile(self):
        folder=Path(__file__).parent/'fixtures/vision-carrier-sequence';config=json.loads((folder/'fixture.json').read_text())
        profile=config['profile'];catalog=JigCatalog(ROOT/'data',profile=profile);catalog.items=config['jigs'];frame=cv2.imread(str(folder/'0.png'));before=deepcopy(profile)
        for key,jig in catalog.items.items():
            if key=='pallet':continue
            with self.subTest(jig=key):
                jig['stl']=str(ROOT/'data/jig_assets'/Path(jig['stl']).name);plane=support_plane_profile(profile,jig);mesh=catalog.mesh(key)
                prepared=DetectionFrame(frame,plane);region=detection_region(prepared,jig['roi'])
                self.assertLess(region.frame.size,frame.size)
                self.assertEqual(region.profile['_detection_image_area'],1280*720)
                cropped=detect(frame,mesh,jig['roi'],plane)['selected']
                with patch('so101_teach.lens_geometry.detection_region',side_effect=lambda p,r:p):full=detect(frame,mesh,jig['roi'],plane)['selected']
                self.assertIsNotNone(cropped);self.assertIsNotNone(full)
                np.testing.assert_allclose(cropped['metric']['center_xy_mm'],full['metric']['center_xy_mm'],atol=.3)
                np.testing.assert_allclose(cropped['center_px'],full['center_px'],atol=.5)
        self.assertEqual(profile,before)
    def test_tight_region_does_not_change_relative_image_area_limit(self):
        from tests.test_carrier_direction import CarrierDirectionTests
        helper=CarrierDirectionTests();helper.setUpClass();frame,profile=helper.synthetic(25,loaded=True)
        original=detect(frame,helper.mesh,profile=profile)['selected'];quad=np.array(original['quad']);lo=quad.min(0)-8;hi=quad.max(0)+8
        roi=np.array([lo,[hi[0],lo[1]],hi,[lo[0],hi[1]]])/[800,600]
        result=detect(frame,helper.mesh,roi.tolist(),profile)
        self.assertIsNotNone(result['selected']);np.testing.assert_allclose(result['selected']['metric']['center_xy_mm'],original['metric']['center_xy_mm'],atol=.3)
        cut=roi.copy();cut[:,0]=np.maximum(cut[:,0],.5)
        self.assertIsNone(detect(frame,helper.mesh,cut.tolist(),profile)['selected'])
