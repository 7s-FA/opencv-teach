import json,unittest
from copy import deepcopy
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import detect,stl_profile,project_plane
from so101_teach.height_reference import support_plane_profile
from so101_teach.jig_consensus import StableCandidate

class CarrierDirectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mesh={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
        folder=Path(__file__).parent/'fixtures/vision-assembly';cfg=json.loads((folder/'fixture.json').read_text());cls.j=cfg['jigs']['b5ebdb54807c441c8267c15d77713b6a'];cls.profile=support_plane_profile(cfg['profile'],cls.j);cls.frame=cv2.imread(str(folder/'current.png'))
    def synthetic(self,yaw=0,*,loaded=False,marker=True,duplicate=False,radius_scale=1.):
        p={'table_z_mm':-4.8,'intrinsics':{'size':[800,600],'K':[[700,0,400],[0,700,300],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,600],[0,0,0,1]]}}
        f=np.full((600,800,3),55,np.uint8);a=np.deg2rad(yaw);rot=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]);quad=np.array([[-110,-74],[110,-74],[110,74],[-110,74]])
        cv2.fillConvexPoly(f,np.rint(project_plane(quad@rot.T,p,0)).astype(int),(230,230,230))
        if loaded:
            for x in [-72,0,72]:
                for y in [-36,36]:
                    q=np.array([[-22,-22],[22,-22],[22,22],[-22,22]])+[x,y]
                    cv2.fillConvexPoly(f,np.rint(project_plane(q@rot.T,p,15)).astype(int),(155,80,190))
        if marker:
            hole=self.mesh['assembly']['orientation_hole'];angles=np.linspace(0,2*np.pi,96,endpoint=False);circle=np.c_[np.cos(angles),np.sin(angles)]
            for sign in ([1,-1] if duplicate else [1]):
                center=np.array(hole['center_mm'])*sign
                for radius,color in [(hole['outer_radius_mm']*radius_scale,(250,250,250)),(hole['inner_radius_mm']*radius_scale,(80,80,80))]:
                    uv=project_plane((circle*radius+center)@rot.T,p,hole['z_mm']-4.8);cv2.fillConvexPoly(f,np.rint(uv).astype(int),color)
        return f,p
    def test_fixed_circle_is_extracted_from_A_fixture_not_product_geometry(self):
        h=self.mesh['assembly']['orientation_hole'];np.testing.assert_allclose(h['center_mm'],[-72,-36],atol=.001);self.assertAlmostEqual(h['inner_radius_mm'],9.,places=3);self.assertAlmostEqual(h['z_mm'],26.5)
    def test_full_turn_rotations_keep_correct_model_heading_loaded_and_empty(self):
        for yaw in [0,25,89,179,181,269,359]:
            for loaded in [False,True]:
                with self.subTest(yaw=yaw,loaded=loaded):
                    f,p=self.synthetic(yaw,loaded=loaded);r=detect(f,self.mesh,profile=p);c=r['selected']
                    self.assertIsNotNone(c,r['status']);self.assertEqual(c['symmetry_deg'],360)
                    self.assertLess(abs((c['metric']['yaw_deg']+c['metric']['mesh_yaw_offset_deg']-yaw+180)%360-180),2)
    def test_real_loaded_hole_resolves_the_wrong_half_turn_without_moving_boundary(self):
        base=deepcopy(self.mesh);base['assembly'].pop('orientation_hole');old=detect(self.frame,base,self.j['roi'],self.profile)['selected']
        c=detect(self.frame,self.mesh,self.j['roi'],self.profile)['selected'];self.assertTrue(c['orientation_verified']);self.assertEqual(c['symmetry_deg'],360)
        self.assertAlmostEqual(c['metric']['yaw_deg'],old['metric']['yaw_deg']);self.assertEqual(c['metric']['mesh_yaw_offset_deg'],180.)
        stable=StableCandidate(3)
        for t in (0,.8,1.6,2.4):stable.update({'selected':c,'candidates':[c],'status':'shape_match'},t,profile=self.profile,mesh=self.mesh)
        accepted=stable.finish(3)['selected'];self.assertAlmostEqual(accepted['metric']['yaw_deg'],old['metric']['yaw_deg']);self.assertEqual(accepted['metric']['mesh_yaw_offset_deg'],180.)
        np.testing.assert_allclose(c['center_px'],old['center_px'],atol=.001)
        np.testing.assert_allclose(c['orientation_hole_px'],[1000,397],atol=12)
    def test_exposure_changes_preserve_full_heading(self):
        reference=detect(self.frame,self.mesh,self.j['roi'],self.profile)['selected']['metric']['yaw_deg']
        for alpha,beta in [(.65,0),(1.,-20),(1.2,10)]:
            f=np.clip(self.frame.astype(float)*alpha+beta,0,255).astype('uint8');c=detect(f,self.mesh,self.j['roi'],self.profile)['selected'];self.assertIsNotNone(c)
            self.assertLess(abs((c['metric']['yaw_deg']-reference+180)%360-180),2)
    def test_missing_duplicated_or_wrong_size_circle_never_adopts_arbitrary_direction(self):
        for kw in [{'marker':False},{'duplicate':True},{'radius_scale':1.6}]:
            f,p=self.synthetic(25,**kw);r=detect(f,self.mesh,profile=p);self.assertIsNone(r['selected']);self.assertEqual(r['status'],'orientation_unconfirmed')
            stable=StableCandidate(3)
            for t in [0,.7,1.4,2.1,2.8,3.5]:self.assertIsNone(stable.update(r,t)['selected'])
    def test_temporal_median_does_not_fold_full_heading_to_180(self):
        f,p=self.synthetic(209,loaded=True);r=detect(f,self.mesh,profile=p);stable=StableCandidate(3)
        for t in [0,.7,1.4,2.1,2.8,3.5]:out=stable.update(r,t,profile=p,mesh=self.mesh)
        self.assertIsNotNone(out['selected']);self.assertAlmostEqual(out['selected']['metric']['mesh_yaw_offset_deg'],180);self.assertLess(abs(out['selected']['metric']['yaw_deg']-29),2);self.assertEqual(out['selected']['symmetry_deg'],360)
