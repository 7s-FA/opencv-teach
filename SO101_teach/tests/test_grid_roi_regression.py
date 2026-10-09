"""The user's ROI must not alter sampling of visible grid divider lines."""
import json,time,unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.vision import detect
from so101_teach.vision_service import MultiDetector
from so101_teach.roi_geometry import roi_mask

class GridROIRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).parent/'fixtures/vision-roi-grid';cfg=json.loads((folder/'fixture.json').read_text());cls.frame=cv2.imread(str(folder/'current.png'));cls.profile=cfg['profile']
        cls.catalog=JigCatalog(ROOT/'data',profile=cls.profile);cls.catalog.items=cfg['jigs']
        for j in cls.catalog.items.values():j['stl']=str(ROOT/'data/jig_assets'/Path(j['stl']).name)
        cls.key='b5ebdb54807c441c8267c15d77713b6a';cls.jig=cls.catalog.items[cls.key];cls.mesh=cls.catalog.mesh(cls.key);cls.plane=support_plane_profile(cls.profile,cls.jig)
    def test_existing_roi_detects_same_carrier_as_unrestricted_image(self):
        inside=detect(self.frame,self.mesh,self.jig['roi'],self.plane);full=detect(self.frame,self.mesh,None,self.plane)
        item=inside['selected'];self.assertIsNotNone(item)
        self.assertTrue(item['grid_verified']);self.assertTrue(item['orientation_verified']);self.assertTrue(item['shape_match'])
        np.testing.assert_allclose(item['metric']['center_xy_mm'],full['selected']['metric']['center_xy_mm'],atol=.1)
        mask=roi_mask(self.jig['roi'],1280,720);foot=np.zeros_like(mask);cv2.fillConvexPoly(foot,np.rint(item['quad']).astype(np.int32),255)
        self.assertFalse(np.any((foot>0)&(mask==0)))
    def test_outside_or_clipped_roi_cannot_be_recovered_from_full_image_edges(self):
        for roi in ([[.1,.1],[.55,.1],[.55,.6],[.1,.6]], [[.78,.4],[1.,.4],[1.,1.],[.78,1.]]):
            self.assertIsNone(detect(self.frame,self.mesh,roi,self.plane)['selected'])
    def test_recorded_frames_reach_consensus_for_required_carrier_within_first_attempt(self):
        detector=MultiDetector(self.catalog,self.profile);detector.refresh()
        with patch('so101_teach.vision_service.time.monotonic',return_value=100.):detector.clear()
        for at in (100.5,101.2,102.0):
            with patch('so101_teach.vision_service.time.monotonic',return_value=at):result=detector.process(self.frame)
        result=detector.finish_observation(result,103.01)['by_jig'][self.key]
        self.assertIsNotNone(result['selected']);self.assertEqual(result['acquisition_completed_attempts'],1)
        self.assertGreaterEqual(result['adoption_inliers'],2);self.assertGreater(result['pose_measured_at'],100)
