import unittest,numpy as np
from tests.fixtures import load_profile
from so101_teach.vision import fit_planar_rectangle,world_xy,project_plane
class PlanarDetectionTests(unittest.TestCase):
    def test_rotations_height_uncertainty_and_minor_corner_noise(self):
        p,_,_=load_profile();q=np.array([[-110,-74],[110,-74],[110,74],[-110,74]],float)
        for a in (0,25,89,130):
            t=np.radians(a);R=np.array([[np.cos(t),-np.sin(t)],[np.sin(t),np.cos(t)]])
            truth=q@R.T+[230,-130]
            for dz in (-10,0,10):
                observed=world_xy(project_plane(truth,p,12.6+dz),p,12.6)
                observed[0]+=[1,-1]
                fit=fit_planar_rectangle(observed,[220,148],p,12.6)
                self.assertIsNotNone(fit)
                edges=np.roll(fit['xy'],-1,axis=0)-fit['xy']
                self.assertAlmostEqual(float(edges[0]@edges[1]),0,places=5)
                angle=np.degrees(np.arctan2(*edges[0][::-1]))
                self.assertLess(abs((angle-a+90)%180-90),1)
    def test_shear_squash_and_wrong_size_are_not_forced_into_good_pose(self):
        p,_,_=load_profile();q=np.array([[0,0],[70,0],[70,70],[0,70]],float)
        for bad in (q*[1,.7],q*1.2,q+[[0,0],[0,0],[14,0],[14,0]]):
            self.assertIsNone(fit_planar_rectangle(bad,[70,70],p,12.6))
