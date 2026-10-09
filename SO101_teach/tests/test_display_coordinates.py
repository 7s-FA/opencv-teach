import unittest
import numpy as np
from so101_teach.display_coordinates import display_position,display_pose,display_delta
from so101_teach.geometry import transform_jig_pose


class DisplayCoordinatesTests(unittest.TestCase):
    def test_right_up_mapping_origin_and_height(self):
        self.assertEqual(display_position([163.4,-79.5,20]),(79.5,163.4,20))
        self.assertEqual(display_position([0,0]),(0,0))
        self.assertEqual(display_pose([0,0,270]),(0,0,0))
        self.assertEqual(display_pose([0,0,0]),(0,0,90))
        self.assertEqual(display_delta([20,-5,3]),(5,20,3))
    def test_same_rotation_basis_for_jig_and_tcp_preserves_relative_motion(self):
        tcp=np.eye(4);tcp[:3,3]=[240,-100,30];old=[200,-90,20];new=[220,-85,30]
        target=transform_jig_pose(tcp,old,new)
        basis=np.eye(4);basis[:2,:2]=[[0,-1],[1,0]]
        shown=transform_jig_pose(basis@tcp,display_pose(old),display_pose(new))
        np.testing.assert_allclose(shown,basis@target,atol=1e-10)
        self.assertEqual(display_pose([1,2,89],90)[2],89)
