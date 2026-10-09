import unittest
from unittest.mock import patch
from copy import deepcopy
from pathlib import Path
import cv2,numpy as np,tempfile
from so101_teach.domain import ROOT
from tests.fixtures import load_profile
from so101_teach.camera_calibration import board_points,validate_intrinsics,board_extrinsics,calibrate_images
from so101_teach.vision_service import MultiDetector
from so101_teach.configuration import JigCatalog
class CameraToolsTests(unittest.TestCase):
    def test_board_uses_internal_points_and_twenty_mm_spacing(self):
        points=board_points(13,9,20);self.assertEqual(points.shape,(117,3));np.testing.assert_allclose(points[-1],[240,160,0])
    def test_invalid_intrinsics_rejected(self):
        for bad in ({'K':[[1]],'D':[0]*5,'size':[1280,720]},{'K':np.eye(3).tolist(),'D':[float('nan')]*5,'size':[1280,720]}):
            with self.assertRaises(ValueError):validate_intrinsics(bad)
    def test_board_transform_matches_known_camera_pose(self):
        K=np.array([[900.,0,640],[0,900,360],[0,0,1]]);D=np.zeros(5);obj=board_points();rv=np.array([.1,.2,.03]);tv=np.array([-120.,-80.,600.]);pts,_=cv2.projectPoints(obj,rv,tv,K,D)
        frame=np.zeros((720,1280,3),np.uint8)
        with patch('so101_teach.camera_calibration.corners',return_value=pts):out=board_extrinsics(frame,{'K':K.tolist(),'D':D.tolist(),'size':[1280,720]},[100,20,0],30,row_sign=-1)
        camera_board=np.eye(4);camera_board[:3,:3]=cv2.Rodrigues(rv)[0];camera_board[:3,3]=tv
        T=np.array(out['base_from_camera'])@camera_board;np.testing.assert_allclose(T[:3,3],[100,20,0],atol=.001)
    def test_legacy_heading_ignored_and_latches_are_independent(self):
        profile,_,_=load_profile();frame=cv2.imread(str(ROOT/'verification/pallet-current.jpg'))
        with tempfile.TemporaryDirectory() as d:
            c=JigCatalog(d);a={**c.items['pallet'],'heading_set':True,'yaw_deg':160.};c.save(a);b=c.duplicate('pallet');b['roi']=[0,0,.3,.3];c.save(b)
            detector=MultiDetector(c,profile);clock=[100.]
            with patch('so101_teach.vision_service.time.monotonic',side_effect=lambda:clock[0]):
                for at in (100.,101.,102.,103.1):clock[0]=at;out=detector.process(frame)
            self.assertIsNotNone(out['by_jig']['pallet']['selected']);self.assertIsNone(out['by_jig'][b['id']]['selected'])
            yaw=out['selected']['metric']['yaw_deg'];self.assertGreaterEqual(yaw,0);self.assertLess(yaw,90);self.assertNotIn('yaw_deg',c.items['pallet']);self.assertNotIn('heading_set',c.items['pallet'])
            detector.freeze(True);held=detector.process(np.zeros_like(frame));self.assertEqual(held['selected']['metric']['yaw_deg'],yaw);self.assertFalse(detector.clear())
    def test_failed_calibration_does_not_return_empty_parameters(self):
        with self.assertRaises(ValueError):calibrate_images([])
