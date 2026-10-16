"""Measured outer-rim refinement when a loaded part hides half of one side."""
from copy import deepcopy
import json,unittest
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import stl_profile
from so101_teach.height_reference import support_plane_profile
from so101_teach.lens_geometry import DetectionFrame
from so101_teach.pose_refinement import refine_outer_boundary
from so101_teach.orientation_model import orient_carrier
from so101_teach.roi_geometry import roi_mask
from so101_teach.inspection_geometry import anchors_for
from so101_teach.episode_inspection import fixed_jig_results
from so101_teach.shape_inspection import ShapeInspector

class OccludedCarrierRimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/carrier-occluded-rim-20261011'
        cls.cfg=json.loads((cls.folder/'fixture.json').read_text());cls.key='b5ebdb54807c441c8267c15d77713b6a'
        cls.jig=cls.cfg['catalog'][cls.key];cls.profile=support_plane_profile(cls.cfg['profile'],cls.jig)
        cls.mesh={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
        cls.reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
    def prepared(self,record):
        frame=cv2.imread(str(self.folder/record['image']));p=DetectionFrame(frame,self.profile);c=deepcopy(record['candidate'])
        c['quad']=cv2.undistortPoints(np.asarray(c['quad'],float)[:,None,:],p.K,p.D,P=p.newK).reshape(-1,2).tolist()
        c['center_px']=cv2.undistortPoints(np.asarray([c['center_px']],float)[:,None,:],p.K,p.D,P=p.newK).reshape(2).tolist()
        return frame,p,{'selected':c,'candidates':[c],'status':'shape_match'}
    def test_visible_three_sides_and_partial_fourth_correct_seating_without_relaxing_it(self):
        inspector=ShapeInspector()
        for record in self.cfg['frames']:
            with self.subTest(image=record['image']):
                frame,p,result=self.prepared(record)
                out=refine_outer_boundary(p.frame,self.mesh,p.profile,p.mask(self.jig['roi']),p.mask(None),result)
                self.assertIsNotNone(out)
                out=p.restore(out);out=orient_carrier(frame,self.mesh,roi_mask(self.jig['roi'],frame.shape[1],frame.shape[0]),self.profile,out)
                c=out['selected'];self.assertIsNotNone(c)
                self.assertTrue(c['observed_boundary_refined']);self.assertTrue(c['orientation_verified'])
                self.assertGreaterEqual(sorted(c['boundary_coverage'])[1],.85)
                self.assertLessEqual(c['boundary_corner_error_mm'],2.)
                self.assertLessEqual(abs(c['boundary_scale']-1),.03)
                fixed={self.key:{'pose':[*c['metric']['center_xy_mm'],c['metric']['yaw_deg']],'mesh_yaw_offset_deg':c['metric']['mesh_yaw_offset_deg'],'symmetry_deg':360}}
                anchors,_=anchors_for(frame,{},fixed_jig_results(fixed),self.cfg['catalog'],self.cfg['profile'],self.reference,'운반용 지그','B',self.cfg['placement'])
                rows=inspector.inspect(frame,anchors,self.cfg['profile']);middle=next(r for r in rows if r['id']=='b_tpu')
                self.assertEqual(middle['state'],'present');self.assertEqual(middle['quality'],'normal',middle['reason'])
                self.assertEqual(middle['center_tolerance_mm'],3.)
                self.assertLessEqual(middle['center_error_mm'],3.)
                self.assertTrue(all(r['quality']=='normal' for r in rows))
    def test_entire_missing_side_cannot_be_invented(self):
        frame,p,result=self.prepared(self.cfg['frames'][0]);q=np.asarray(result['selected']['quad']);a,b=q[2],q[3]
        missing=p.frame.copy();cv2.line(missing,tuple(np.rint(a).astype(int)),tuple(np.rint(b).astype(int)),(55,55,55),50)
        out=refine_outer_boundary(missing,self.mesh,p.profile,p.mask(self.jig['roi']),p.mask(None),result)
        self.assertIsNone(out)
    def test_source_replacement_does_not_change_the_detection_plane_or_fitted_pose(self):
        prior=self.cfg['previous_mesh']
        for field in ('size_mm','low_mm','rim_z_mm'):np.testing.assert_allclose(prior[field],self.mesh[field],atol=1e-6)
        np.testing.assert_allclose(prior['assembly']['orientation_hole']['center_mm'],self.mesh['assembly']['orientation_hole']['center_mm'],atol=1e-6)
        frame,p,result=self.prepared(self.cfg['frames'][0]);outputs=[]
        for mesh in (prior,self.mesh):
            out=refine_outer_boundary(p.frame,mesh,p.profile,p.mask(self.jig['roi']),p.mask(None),deepcopy(result));self.assertIsNotNone(out)
            outputs.append(out['selected']['metric']['center_xy_mm'])
        np.testing.assert_allclose(*outputs,atol=1e-6)
