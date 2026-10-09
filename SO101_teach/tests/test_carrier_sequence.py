"""Actual consecutive frames must not lose the carrier to an early scale match."""
import json,unittest
from pathlib import Path
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.vision_service import MultiDetector

class CarrierSequenceTests(unittest.TestCase):
    def test_distinct_live_frames_confirm_same_grid_and_heading(self):
        folder=Path(__file__).parent/'fixtures/vision-carrier-sequence'
        config=json.loads((folder/'fixture.json').read_text());profile=config['profile']
        catalog=JigCatalog(ROOT/'data',profile=profile);catalog.items=config['jigs']
        for item in catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        detector=MultiDetector(catalog,profile);detector.refresh()
        with patch('so101_teach.vision_service.time.monotonic',return_value=100.):detector.clear()
        centers=[]
        for at,index in zip((100.3,100.8,101.3,101.8,102.3,102.8),(0,2,5,7,9,6)):
            with self.subTest(frame=index),patch('so101_teach.vision_service.time.monotonic',return_value=at):
                result=detector.process(cv2.imread(str(folder/f'{index}.png')))
                raw=result['live_by_jig']['b5ebdb54807c441c8267c15d77713b6a'];item=raw['selected']
                self.assertIsNotNone(item,raw['status']);self.assertTrue(item['grid_verified']);self.assertTrue(item['orientation_verified'])
                self.assertLessEqual(item['orientation_hole_alignment_error_mm'],4)
                self.assertLess(abs(item['metric']['yaw_deg']-29.35),.5)
                centers.append(item['metric']['center_xy_mm'])
        self.assertLess(np.ptp(centers,axis=0).max(),.3)
        result=detector.finish_observation(result,103.01)['by_jig']['b5ebdb54807c441c8267c15d77713b6a']
        self.assertIsNotNone(result['selected']);self.assertEqual(result['acquisition_completed_attempts'],1)
        self.assertEqual(result['adoption_inliers'],6)
