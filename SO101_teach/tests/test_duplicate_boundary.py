import json,unittest
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import stl_profile,detect,project_plane
from so101_teach.height_reference import support_plane_profile
from so101_teach.lens_geometry import DetectionFrame
from so101_teach.pose_refinement import merge_refined_boundaries

class DuplicateBoundaryTests(unittest.TestCase):
    def test_two_recorded_hypotheses_converge_on_one_independently_observed_rim(self):
        folder=Path(__file__).parent/'fixtures/adoption-latency';cfg=json.loads((folder.parent/'vision-assembly/fixture.json').read_text());j=cfg['jigs']['b5ebdb54807c441c8267c15d77713b6a']
        m={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
        f=cv2.imread(str(folder/'frame.jpg'));prepared=DetectionFrame(f,support_plane_profile(cfg['profile'],j));r=json.loads((folder/'ambiguous.json').read_text())
        for c in r['candidates']:c['quad']=cv2.undistortPoints(np.asarray(c['quad']).reshape(-1,1,2),prepared.K,prepared.D,P=prepared.newK).reshape(-1,2).tolist()
        out=merge_refined_boundaries(prepared.frame,m,prepared.profile,prepared.mask(j['roi']),prepared.mask(None),r)
        self.assertEqual(out['duplicate_boundary_hypotheses'],2);self.assertEqual(out['status'],'shape_match');self.assertTrue(out['selected']['observed_boundary_refined'])
        self.assertGreaterEqual(min(out['selected']['edge_support']),.6);self.assertGreaterEqual(sorted(out['selected']['edge_support'])[1],.9)
        self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['selected'])
    def test_two_separate_visible_carriers_remain_ambiguous(self):
        p={'table_z_mm':-4.8,'intrinsics':{'size':[800,600],'K':[[600,0,400],[0,600,300],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,500],[0,0,0,1]]}}
        m={'size_mm':[220,148,6],'low_mm':[-110,-74,0],'rim_z_mm':4.8,'shape':'rectangle','method':'edges','holes':[]};f=np.full((600,800,3),30,np.uint8)
        for center in [-125,125]:
            q=np.array([[-110,-74],[110,-74],[110,74],[-110,74]])+[center,0];cv2.fillConvexPoly(f,np.rint(project_plane(q,p,0)).astype(int),(230,230,230))
        r=detect(f,m,profile=p);self.assertEqual(r['status'],'ambiguous');self.assertIsNone(r['selected']);self.assertEqual(len(r['candidates']),2)
