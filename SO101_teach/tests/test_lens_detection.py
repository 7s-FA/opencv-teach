from copy import deepcopy
from pathlib import Path
import json,unittest
import cv2,numpy as np
from so101_teach.vision import detect,project_plane
from so101_teach.lens_geometry import DetectionFrame

class LensDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root=Path(__file__).parent/'fixtures/vision-lens';cls.frame=cv2.imread(str(root/'input.jpg'));cls.fixture=json.loads((root/'fixture.json').read_text())
    def config(self,key):
        j=self.fixture['jigs'][key]
        return {**j['mesh'],'shape':j['shape'],'method':j['method']},j['roi'],deepcopy(self.fixture['profile'])
    def test_carrier_center_and_outer_boundary_follow_observed_edges(self):
        mesh,roi,p=self.config('b5ebdb54807c441c8267c15d77713b6a');out=detect(self.frame,mesh,roi,p);c=out['selected']
        self.assertIsNotNone(c);np.testing.assert_allclose(c['center_px'],[1098,507],atol=2)
        observed=np.float32([[1046,310],[1264,541],[1134,713],[892,462]])
        # Hand-labelled image vertices use the same 5 px tolerance as the
        # assembly boundary fixtures; centre precision is checked separately.
        self.assertLess(np.linalg.norm(np.array(c['quad'])-observed,axis=1).max(),5)
        self.assertEqual(len(c['outline_px']),96);self.assertFalse(out['verified_for_motion'])
    def test_low_contrast_pallet_needs_opening_and_each_outer_side(self):
        mesh,roi,p=self.config('pallet');out=detect(self.frame,mesh,roi,p);c=out['selected']
        self.assertIsNotNone(c);self.assertTrue(c['model_recovered'])
        np.testing.assert_allclose(c['center_px'],[291,334.5],atol=3)
        self.assertGreaterEqual(min(c['edge_support'][:4]),.4)
        self.assertGreaterEqual(min(c['edge_support'][4:]),.55)
        # Keep the actual opening and its shadow, but remove the outer rim.
        opening_only=np.full_like(self.frame,200);opening_only[306:364,265:319]=self.frame[306:364,265:319]
        self.assertIsNone(detect(opening_only,mesh,roi,p)['selected'])
    def test_cut_roi_empty_scene_and_wrong_size_cannot_recover(self):
        mesh,roi,p=self.config('pallet')
        for image,m,r in [(self.frame,mesh,[.20,.42,.24,.52]),(np.full_like(self.frame,200),mesh,roi),(self.frame,{**mesh,'size_mm':[100,100,25]},roi)]:
            self.assertIsNone(detect(image,m,r,p)['selected'])
    def test_camera_identity_and_resolution_require_matching_calibration(self):
        mesh,roi,p=self.config('pallet');p['camera']['source']='D435'
        self.assertEqual(detect(self.frame,mesh,roi,p)['status'],'calibration_mismatch')
        _,_,p=self.config('pallet');small=cv2.resize(self.frame,(640,360))
        self.assertEqual(detect(small,mesh,roi,p)['status'],'calibration_mismatch')
    def test_shared_rectification_keeps_raw_roi_and_plane_height(self):
        mesh,roi,p=self.config('pallet');prepared=DetectionFrame(self.frame,p)
        a=detect(self.frame,mesh,roi,p,prepared=prepared);b=detect(self.frame,mesh,roi,p)
        np.testing.assert_allclose(a['selected']['center_px'],b['selected']['center_px'])
        # The public result is raw-camera pixels, not coordinates of the shrunken image.
        self.assertLess(a['selected']['center_px'][0],roi[2]*1280)
        self.assertGreater(a['selected']['center_px'][0],roi[0]*1280)
        self.assertTrue(np.all(np.asarray(prepared.profile['intrinsics']['D'])==0))
        self.assertNotEqual(p['intrinsics']['D'],prepared.profile['intrinsics']['D'])
    def test_synthetic_curved_edges_at_left_center_and_right(self):
        mesh,_,_=self.config('pallet')
        p={'table_z_mm':0,'intrinsics':{'size':[1280,720],'K':[[900,0,640],[0,900,360],[0,0,1]],'D':[-.35,.10,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,500],[0,0,0,1]]}}
        t=np.deg2rad(27);R=np.array([[np.cos(t),-np.sin(t)],[np.sin(t),np.cos(t)]])
        for center in ([-220,80],[0,0],[220,-80]):
            frame=np.full((720,1280,3),100,np.uint8)
            for size,color in [(70,220),(70*.4033333333,100)]:
                corners=np.array([[-1,-1],[1,-1],[1,1],[-1,1]])*size/2
                border=np.concatenate([a+(b-a)*np.linspace(0,1,40)[:,None] for a,b in zip(corners,np.roll(corners,-1,axis=0))])
                uv=project_plane(border@R.T+center,p,20);cv2.fillPoly(frame,[np.rint(uv).astype(np.int32)],(color,)*3)
            result=detect(frame,mesh,profile=p);self.assertIsNotNone(result['selected'],center)
            np.testing.assert_allclose(result['selected']['center_px'],project_plane([center],p,20)[0],atol=2)
            np.testing.assert_allclose(result['selected']['metric']['center_xy_mm'],center,atol=1.5)
