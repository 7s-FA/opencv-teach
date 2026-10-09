import json,unittest
from copy import deepcopy
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.height_reference import support_plane_profile
from so101_teach.vision import detect,stl_profile

class RaisedRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(__file__).parent/'fixtures/vision-raised-recovery'
        cfg=json.loads((cls.folder/'fixture.json').read_text());cls.roi=cfg['roi']
        cls.profile=support_plane_profile(cfg['profile'],cfg)
        cls.mesh={**stl_profile(ROOT/'assets/jigs/pallet.stl'),'shape':'ring','method':'edges'}
        cls.frame=cv2.imread(str(cls.folder/'weak-rim-1.png'))
    def test_real_faint_rims_recover_with_four_independent_corners(self):
        poses=[]
        for name in ('weak-rim-1.png','weak-rim-2.png'):
            result=detect(cv2.imread(str(self.folder/name)),self.mesh,self.roi,self.profile)
            chosen=result['selected'];self.assertIsNotNone(chosen)
            self.assertTrue(chosen['raised_edge_recovery']);self.assertTrue(chosen['raised_features_verified'])
            self.assertGreaterEqual(min(chosen['raised_corner_support']),.75)
            self.assertGreaterEqual(min(chosen['edge_support'][:4]),.25)
            self.assertGreaterEqual(sorted(chosen['edge_support'][4:])[1],.75)
            np.testing.assert_allclose(chosen['center_px'],[431,188],atol=3)
            self.assertFalse(result['verified_for_motion']);self.assertFalse(chosen['metric']['verified'])
            poses.append(chosen['metric'])
        self.assertLess(np.linalg.norm(np.subtract(poses[0]['center_xy_mm'],poses[1]['center_xy_mm'])),1)
        self.assertLess(abs(poses[0]['yaw_deg']-poses[1]['yaw_deg']),1)
    def test_faint_fit_cannot_recover_without_registered_raised_geometry(self):
        mesh={**self.mesh,'raised_edges_mm':[]}
        self.assertIsNone(detect(self.frame,mesh,self.roi,self.profile)['selected'])
    def test_misplaced_corner_geometry_does_not_corroborate_faint_rim(self):
        mesh=deepcopy(self.mesh)
        # Outer rim and opening stay correct; three matching corners cannot
        # stand in for the fourth corner required by this recovery path.
        for edge in mesh['raised_edges_mm'][:2]:
            for point in edge:point[0]+=12;point[1]+=12
        self.assertIsNone(detect(self.frame,mesh,self.roi,self.profile)['selected'])
    def test_roi_cut_and_wrong_size_remain_rejected(self):
        self.assertIsNone(detect(self.frame,self.mesh,[.30,.19,.36,.35],self.profile)['selected'])
        self.assertIsNone(detect(self.frame,{**self.mesh,'size_mm':[90,90,25]},self.roi,self.profile)['selected'])
    def test_real_frame_with_opening_covered_is_not_recovered(self):
        frame=self.frame.copy()
        # Obscure the visible central opening while retaining outer corners.
        cv2.fillConvexPoly(frame,np.array([[405,172],[440,156],[459,195],[424,211]],np.int32),(235,235,235))
        self.assertIsNone(detect(frame,self.mesh,self.roi,self.profile)['selected'])

if __name__=='__main__':unittest.main()
