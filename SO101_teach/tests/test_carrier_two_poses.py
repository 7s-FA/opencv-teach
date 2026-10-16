"""Loaded carrier must converge on its measured outer rim in both detection paths."""
from copy import deepcopy
import json,unittest
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import stl_profile,detect
from so101_teach.height_reference import support_plane_profile
from so101_teach.lens_geometry import DetectionFrame
from so101_teach.pose_refinement import refine
from so101_teach.jig_consensus import StableCandidate

class CarrierTwoPosesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/carrier-two-poses-20261012'
        cls.cfg=json.loads((cls.folder/'fixture.json').read_text());cls.jig=cls.cfg['jig']
        cls.profile=support_plane_profile(cls.cfg['profile'],cls.jig)
        cls.mesh={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
    def prepared(self,record):
        frame=cv2.imread(str(self.folder/record['file']));p=DetectionFrame(frame,self.profile);c=deepcopy(record['candidate'])
        c['quad']=cv2.undistortPoints(np.asarray(c['quad'],float)[:,None,:],p.K,p.D,P=p.newK).reshape(-1,2).tolist()
        c['center_px']=cv2.undistortPoints(np.asarray([c['center_px']],float)[:,None,:],p.K,p.D,P=p.newK).reshape(2).tolist()
        return p,{'selected':c,'candidates':[c],'status':'shape_match'}
    def test_full_pipeline_consecutive_images_confirm_one_outer_pose(self):
        stable=StableCandidate(3);centers=[]
        records=[r for r in self.cfg['records'] if r['file'] not in ('observed-06.jpg','observed-14.jpg')]
        for i,record in enumerate(records):
            with self.subTest(image=record['file']):
                frame=cv2.imread(str(self.folder/record['file']))
                result=detect(frame,self.mesh,self.jig['roi'],self.profile);c=result['selected']
                self.assertIsNotNone(c,result['status']);self.assertTrue(c['orientation_verified']);self.assertTrue(c['observed_boundary_refined'])
                center=c['metric']['center_xy_mm'];centers.append(center)
                self.assertLess(np.linalg.norm(np.asarray(center)-self.cfg['expected_center_mm']),.3)
                stable.update(result,i*.6,profile=self.profile,mesh=self.mesh)
        self.assertLess(float(np.ptp(centers,axis=0).max()),.2)
        accepted=stable.finish(3)
        self.assertIsNotNone(accepted['selected']);self.assertEqual(accepted['adoption_inliers'],5)
    def test_contour_candidates_use_the_same_measured_boundary(self):
        for record in self.cfg['records']:
            if record['file'] not in ('observed-06.jpg','observed-14.jpg'):continue
            with self.subTest(image=record['file']):
                p,result=self.prepared(record);self.assertFalse(result['selected'].get('model_recovered'))
                out=refine(p.frame,self.mesh,p.profile,p.mask(self.jig['roi']),p.mask(None),result);c=out['selected']
                self.assertIsNotNone(c);self.assertTrue(c['observed_boundary_refined'])
                self.assertLess(np.linalg.norm(np.asarray(c['metric']['center_xy_mm'])-self.cfg['expected_center_mm']),.4)
    def test_missing_outer_side_does_not_fall_back_to_an_inner_pose(self):
        record=next(r for r in self.cfg['records'] if r['file']=='observed-02.jpg');p,result=self.prepared(record)
        self.assertTrue(result['selected']['model_recovered'])
        a,b=np.asarray(result['selected']['quad'])[2:4];frame=p.frame.copy()
        cv2.line(frame,tuple(np.rint(a).astype(int)),tuple(np.rint(b).astype(int)),(55,55,55),50)
        out=refine(frame,self.mesh,p.profile,p.mask(self.jig['roi']),p.mask(None),result)
        self.assertIsNone(out['selected']);self.assertTrue(out['boundary_unverified'])
