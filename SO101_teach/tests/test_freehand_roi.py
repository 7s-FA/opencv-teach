import unittest
import numpy as np
from so101_teach.roi_geometry import roi_vertices,roi_mask,freehand_roi
from so101_teach.vision import quad_inside_mask

class FreehandROITests(unittest.TestCase):
    def test_concave_region_preserves_notch_instead_of_convex_hull(self):
        points=[[.1,.1],[.9,.1],[.9,.4],[.4,.4],[.4,.9],[.1,.9]]
        region=freehand_roi(points,1280,720);self.assertEqual(len(region),6)
        mask=roi_mask(region,100,100)
        self.assertEqual(mask[20,80],255);self.assertEqual(mask[80,20],255);self.assertEqual(mask[70,70],0)
        self.assertFalse(quad_inside_mask(np.array([[20,20],[80,20],[80,80],[20,80]]),mask))
    def test_mask_interior_hole_cannot_pass_using_only_outer_samples(self):
        mask=np.full((100,100),255,np.uint8);mask[40:60,40:60]=0
        self.assertFalse(quad_inside_mask(np.array([[10,10],[90,10],[90,90],[10,90]]),mask))
    def test_border_stroke_clamps_and_closes_without_duplicate_endpoint(self):
        region=freehand_roi([[.6,-.2],[1.2,-.2],[1.2,.8],[.6,.8],[.6,-.2]],1280,720)
        self.assertEqual(len(region),4);self.assertEqual(np.asarray(region).max(),1.)
        self.assertEqual(np.asarray(region).min(),0.)
    def test_self_crossing_and_tiny_regions_rejected(self):
        for points in ([[.1,.1],[.8,.8],[.8,.1],[.1,.8]],[[.1,.1],[.101,.1],[.101,.101]]):
            with self.assertRaises(ValueError):freehand_roi(points,1280,720)
    def test_dense_round_stroke_is_simplified_but_not_replaced_by_box(self):
        angle=np.linspace(0,2*np.pi,500);points=np.c_[.5+.3*np.cos(angle),.5+.2*np.sin(angle)]
        region=freehand_roi(points,1280,720);self.assertLess(len(region),128);self.assertGreater(len(region),8)
        self.assertEqual(roi_mask(region,100,100)[30,20],0)
