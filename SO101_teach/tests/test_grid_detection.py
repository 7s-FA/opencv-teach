import json,tempfile,unittest
from copy import deepcopy
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import detect,project_plane
from so101_teach.grid_model import grid_layout,grid_hypotheses,matching_grid
from so101_teach.lens_geometry import DetectionFrame
from so101_teach.height_reference import support_plane_profile
from so101_teach.configuration import JigCatalog

class GridDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(__file__).parent/'fixtures/vision-grid';cls.cfg=json.loads((cls.folder/'fixture.json').read_text())
        cls.j=cls.cfg['jigs']['b5ebdb54807c441c8267c15d77713b6a']
        cls.mesh={**cls.j['mesh'],'shape':'rectangle','method':'grid'}
        cls.profile=support_plane_profile(cls.cfg['profile'],cls.j)
    def synthetic(self,angle=0,*,outer=True,grid=True,wrong_spacing=False):
        p={'table_z_mm':-4.8,'intrinsics':{'size':[640,480],'K':[[600,0,320],[0,600,240],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,500],[0,0,0,1]]}}
        a=np.deg2rad(angle);rotation=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]);image=np.full((480,640,3),60,np.uint8)
        def line(x,y):
            points=np.rint(project_plane(np.array([x,y])@rotation.T,p,0)).astype(int)
            cv2.line(image,tuple(points[0]),tuple(points[1]),(220,220,220),2)
        quad=np.array([[-110,-74],[110,-74],[110,74],[-110,74]])
        if outer:
            for x,y in zip(quad,np.roll(quad,-1,axis=0)):line(x+(y-x)*.06,y-(y-x)*.06)
        if grid:
            layout=grid_layout(self.mesh)
            for x in layout['points'][:,0]:line([x*(.65 if wrong_spacing else 1),-42],[x*(.65 if wrong_spacing else 1),42])
            line([-58,layout['points'][0,1]],[58,layout['points'][0,1]])
        return image,p
    def test_real_loaded_carrier_checks_two_crossings_without_shifting_good_boundary(self):
        frame=cv2.imread(str(self.folder/'current.png'))
        old=detect(frame,{**self.mesh,'method':'edges'},self.j['roi'],self.profile)['selected']
        result=detect(frame,self.mesh,self.j['roi'],self.profile);c=result['selected']
        self.assertTrue(c['grid_verified']);self.assertEqual(len(c['grid_crossings_px']),2)
        np.testing.assert_allclose(c['center_px'],old['center_px'],atol=.01)
        # Pixel locations marked on the raw lens-distorted photograph, not on
        # the rectified processing image.
        points=np.array(c['grid_crossings_px']);reference=np.array([[1062,420],[1124,508]])
        self.assertLess(float(np.linalg.norm(points[:,None]-reference[None,:],axis=2).min(0).max()),6)
        self.assertFalse(result['verified_for_motion']);self.assertEqual(result['grid_direction_period_deg'],180)
    def test_crossings_recover_disconnected_outer_sides_at_multiple_rotations(self):
        for angle in (0,35,95,185):
            with self.subTest(angle=angle):
                frame,p=self.synthetic(angle)
                self.assertIsNone(detect(frame,{**self.mesh,'method':'edges'},profile=p)['selected'])
                result=detect(frame,self.mesh,profile=p);c=result['selected']
                self.assertIsNotNone(c);self.assertTrue(result['grid_seed_recovery']);self.assertTrue(c['grid_verified'])
                self.assertLess(abs((c['metric']['yaw_deg']-angle+90)%180-90),2)
                np.testing.assert_allclose(c['center_px'],[320,240],atol=2)
    def test_grid_alone_wrong_spacing_blank_and_wrong_size_do_not_create_jig(self):
        for options in ({'outer':False},{'wrong_spacing':True},{'grid':False}):
            image,p=self.synthetic(**options);self.assertIsNone(detect(image,self.mesh,profile=p)['selected'])
        image,p=self.synthetic();self.assertIsNone(detect(np.full_like(image,100),self.mesh,profile=p)['selected'])
        self.assertIsNone(detect(image,{**self.mesh,'size_mm':[160,110,6]},profile=p)['selected'])
    def test_one_occluded_intersection_arm_can_use_the_other_three(self):
        image,p=self.synthetic()
        y=grid_layout(self.mesh)['points'][0,1]
        points=np.rint(project_plane([[-60,y],[-42,y]],p,0)).astype(int)
        cv2.line(image,tuple(points[0]),tuple(points[1]),(60,60,60),6)
        result=detect(image,self.mesh,profile=p)
        self.assertIsNotNone(result['selected']);self.assertTrue(result['selected']['grid_verified'])
    def test_user_roi_cut_and_missing_complete_outer_side_remain_rejected(self):
        image,p=self.synthetic();self.assertIsNone(detect(image,self.mesh,[.4,0,1,1],p)['selected'])
        image[145:157,:]=60
        self.assertIsNone(detect(image,self.mesh,profile=p)['selected'])
    def test_axis_match_rejects_quarter_turn_and_does_not_claim_unique_front_back(self):
        frame=cv2.imread(str(self.folder/'current.png'));prepared=DetectionFrame(frame,self.profile)
        hints=grid_hypotheses(prepared.frame,self.mesh,prepared.mask(self.j['roi']),prepared.profile)
        c=detect(frame,self.mesh,self.j['roi'],self.profile)['selected']
        self.assertIsNotNone(matching_grid(c,hints,self.mesh))
        wrong=deepcopy(c);wrong['metric']['yaw_deg']+=90
        self.assertIsNone(matching_grid(wrong,hints,self.mesh))
        opposite=deepcopy(c);opposite['metric']['yaw_deg']+=180
        self.assertIsNotNone(matching_grid(opposite,hints,self.mesh));self.assertEqual(c['symmetry_deg'],180)
    def test_settings_accept_supported_stl_and_reject_other_shapes_before_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog=JigCatalog(tmp,profile=self.cfg['profile']);item={**deepcopy(self.j),'method':'grid'}
            item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
            saved=catalog.save(item);self.assertEqual(saved['method'],'grid');before=catalog.path.read_bytes()
            for changes in ({'shape':'ring'},{'stl':None},{'stl':str(ROOT/'assets/jigs/pallet.stl')}):
                with self.assertRaises(ValueError):catalog.save({**item,**changes})
                self.assertEqual(catalog.path.read_bytes(),before)
