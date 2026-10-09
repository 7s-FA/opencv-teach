from pathlib import Path
from copy import deepcopy
import json,unittest
import cv2,numpy as np
from so101_teach.vision import detect,project_plane
from so101_teach.height_reference import support_plane_profile

class PositionDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).parent/'fixtures/vision-position';cls.frame=cv2.imread(str(root/'input.jpg'));cls.config=json.loads((root/'fixture.json').read_text())
    def values(self,key):
        j=self.config['jigs'][key]
        return deepcopy(j['mesh']),deepcopy(j['roi']),support_plane_profile(self.config['profile'],j)
    def test_rounded_pallet_opening_retains_independent_outer_edge_checks(self):
        m,r,p=self.values('pallet');result=detect(self.frame,m,r,p);c=result['selected']
        self.assertIsNotNone(c);self.assertTrue(c['model_recovered']);self.assertFalse(c.get('partial_visible',False))
        np.testing.assert_allclose(c['center_px'],[431,189],atol=3)
        self.assertGreaterEqual(min(c['edge_support'][:4]),.4);self.assertGreaterEqual(min(c['edge_support'][4:]),.55)
    def test_carrier_clipped_at_camera_border_matches_four_visible_sides(self):
        m,r,p=self.values('b5ebdb54807c441c8267c15d77713b6a');result=detect(self.frame,m,r,p);c=result['selected']
        self.assertIsNotNone(c);self.assertTrue(c['partial_visible']);self.assertGreater(c['visible_fraction'],.99)
        np.testing.assert_allclose(c['center_px'],[1116,516],atol=4);self.assertGreater(max(v[1] for v in c['quad']),720)
        self.assertGreaterEqual(min(c['edge_support']),.65);self.assertFalse(result['verified_for_motion'])
        # Raw distorted image geometry must project back to the same physical plane.
        projected=project_plane([c['metric']['center_xy_mm']],p,p['table_z_mm']+m['rim_z_mm']-m['low_mm'][2])
        np.testing.assert_allclose(c['center_px'],projected[0],atol=1e-3)
    def test_camera_clipping_does_not_allow_crossing_user_roi(self):
        m,_,p=self.values('b5ebdb54807c441c8267c15d77713b6a')
        self.assertIsNone(detect(self.frame,m,[.74,.40,.96,1.],p)['selected'])
    def test_empty_image_and_wrong_dimensions_are_not_inferred(self):
        m,r,p=self.values('b5ebdb54807c441c8267c15d77713b6a')
        self.assertIsNone(detect(np.full_like(self.frame,180),m,r,p)['selected'])
        self.assertIsNone(detect(self.frame,{**m,'size_mm':[160,110,6]},r,p)['selected'])
    def test_missing_outer_side_and_excessive_clipping_are_rejected(self):
        from so101_teach.rectangle_model import rectangle_proposals,quad_visibility
        p={'table_z_mm':0,'intrinsics':{'size':[640,480],'K':[[600,0,320],[0,600,240],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,500],[0,0,0,1]]}}
        mesh={'shape':'rectangle','size_mm':[220,148,6],'low_mm':[0,0,0],'rim_z_mm':4.8,'holes':[]};image=np.full((480,640,3),100,np.uint8);allowed=np.full(image.shape[:2],255,np.uint8)
        q=np.rint(project_plane([[-110,-74],[110,-74],[110,74],[-110,74]],p,4.8)).astype(int)
        cv2.polylines(image,[q],False,(230,230,230),2)
        self.assertEqual(rectangle_proposals(image,mesh,allowed,p,allowed),[])
        self.assertLess(quad_visibility(np.array([[500,100],[780,100],[780,400],[500,400]]),allowed,allowed),.90)
    def test_concave_roi_notch_inside_footprint_still_rejects_model(self):
        from so101_teach.rectangle_model import quad_visibility
        visible=np.full((100,100),255,np.uint8);allowed=visible.copy();allowed[40:60,40:60]=0
        self.assertEqual(quad_visibility(np.array([[10,10],[90,10],[90,90],[10,90]]),allowed,visible),0.)
    def test_new_exposure_detects_faint_pallet_and_no_removed_carrier(self):
        frame=cv2.imread(str(Path(__file__).parent/'fixtures/vision-position/carrier-removed.jpg'))
        m,r,p=self.values('pallet');result=detect(frame,m,r,p);c=result['selected']
        self.assertIsNotNone(c);np.testing.assert_allclose(c['center_px'],[431,189],atol=3)
        self.assertGreaterEqual(sorted(c['edge_support'][:4])[1],.85)
        m,r,p=self.values('b5ebdb54807c441c8267c15d77713b6a');self.assertIsNone(detect(frame,m,r,p)['selected'])
