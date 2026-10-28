"""Current-image visibility, immutable CAD caching and ambiguity safeguards."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach import carrier_cad
from so101_teach.height_reference import support_plane_profile
from so101_teach.roi_geometry import roi_mask
from so101_teach.vision import stl_profile


class CarrierRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mesh={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
        cfg=json.loads((ROOT/'tests/fixtures/inspection-all/fixture.json').read_text())
        jig=cfg['catalog']['b5ebdb54807c441c8267c15d77713b6a']
        cls.profile=support_plane_profile(cfg['profile'],jig)
        cls.allowed=roi_mask(jig['roi'],1280,720)
        cls.candidate={'metric':{'center_xy_mm':[196.79,-130.14],'yaw_deg':209.57},'boundary_scale':1.0198}

    def test_sparse_depth_uses_perspective_and_nearest_surface(self):
        triangle=np.array([[0.,0.],[4.,0.],[0.,4.]])
        points=np.array([[1,1],[2,1],[4,4]])
        depth=carrier_cad.depth_at_pixels(triangle,np.array([2.,4.,4.]),(5,5),points)
        self.assertAlmostEqual(float(depth[1,1]),1/(.5/2+.25/4+.25/4),places=6)
        self.assertTrue(np.isinf(depth[4,4]))
        triangles=np.stack([triangle,triangle]);z=np.array([[2.,4.,4.],[1.,1.,1.]])
        a=carrier_cad.depth_at_pixels(triangles,z,(5,5),points)
        b=carrier_cad.depth_at_pixels(triangles[::-1],z[::-1],(5,5),points)
        np.testing.assert_array_equal(a,b);self.assertEqual(float(a[1,1]),1.)

    def test_empty_visibility_query_stays_unobserved(self):
        depth=carrier_cad.depth_at_pixels(np.array([[0.,0.],[4.,0.],[0.,4.]]),np.ones(3),(5,5),np.empty((0,2),int))
        self.assertTrue(np.isinf(depth).all())
        outside=carrier_cad.depth_at_pixels(np.array([[0.,0.],[1.,0.],[0.,1.]]),np.ones(3),(5,5),np.array([[4,4]]))
        self.assertTrue(np.isinf(outside).all())

    def test_cache_reuses_only_unchanged_verified_mesh(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'carrier.stl';path.write_bytes(Path(self.mesh['stl_path']).read_bytes())
            mesh={**self.mesh,'stl_path':str(path)}
            first=carrier_cad.mesh_features(mesh);second=carrier_cad.mesh_features(mesh)
            self.assertIs(first,second)
            self.assertTrue(all(not a.flags.writeable for a in first))
            path.write_bytes(path.read_bytes()+b'changed')
            with self.assertRaisesRegex(ValueError,'지문'):carrier_cad.mesh_features(mesh)

    def test_blank_and_excluded_current_image_do_not_inherit_heading(self):
        for value in (0,128,255):
            scores=carrier_cad.evaluate(np.full((720,1280,3),value,np.uint8),self.mesh,self.allowed,self.profile,self.candidate)
            self.assertFalse(carrier_cad.verdict(scores)['verified'])
        image=cv2.imread(str(ROOT/'tests/fixtures/inspection-all/faults.jpg'))
        scores=carrier_cad.evaluate(image,self.mesh,np.zeros((720,1280),np.uint8),self.profile,self.candidate)
        self.assertFalse(carrier_cad.verdict(scores)['verified'])

    def test_stable_independent_direction_conflict_is_rejected(self):
        # Strong CAD points to one heading while every junction perturbation
        # supports the opposite heading: neither may override the other.
        groups=lambda score:[{'count':100,'score':score,'support':1.} for _ in range(6)]
        scores=[{'model_yaw':30.,'teaching_heading':210.,'score':.8,'groups':groups(.8)},
                {'model_yaw':210.,'teaching_heading':30.,'score':.4,'groups':groups(.4)}]
        junction={'verified':True,'yaw_deg':210.,'scores':[],'reason':'test'}
        with patch.object(carrier_cad,'evaluate',return_value=scores),patch('so101_teach.junction_orientation.evaluate',return_value=deepcopy(junction)):
            result=carrier_cad.resolve(np.zeros((720,1280,3),np.uint8),self.mesh,self.allowed,self.profile,self.candidate,junction)
        self.assertFalse(result['verified']);self.assertIn('충돌',result['reason'])
