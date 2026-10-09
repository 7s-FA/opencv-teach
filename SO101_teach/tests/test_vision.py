import unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from tests.fixtures import load_profile
from so101_teach.vision import stl_profile,detect,world_xy,rectangle_quality,projected_jig_axes,PoseLatch

class VisionTests(unittest.TestCase):
    def setUp(self):self.profile,_,_=load_profile();self.mesh=stl_profile()
    def test_outer_shape_rejects_rhombus_rectangle_and_wrong_size(self):
        square=np.array([[0,0],[70,0],[70,70],[0,70]],float)
        for angle in (0,20,65,89):
            a=np.deg2rad(angle);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
            self.assertTrue(rectangle_quality(square@R.T,[70,70])['valid'])
        for bad in (square*[1,.7],square*1.3,np.array([[0,0],[70,0],[90,67],[20,67]])):
            self.assertFalse(rectangle_quality(bad,[70,70])['valid'])
    def test_stl_ratio_accepts_rotated_rectangle_but_rejects_same_area_square(self):
        rectangle=np.array([[0,0],[220,0],[220,148],[0,148]],float)
        for angle in (0,35,90,125):
            a=np.deg2rad(angle);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
            for shift in range(4):
                out=rectangle_quality(np.roll(rectangle@R.T,shift,axis=0),[220,148])
                self.assertTrue(out['valid']);self.assertAlmostEqual(out['stl_aspect_ratio'],220/148)
        equal=np.sqrt(220*148)*np.array([[0,0],[1,0],[1,1],[0,1]])
        self.assertFalse(rectangle_quality(equal,[220,148])['valid'])
        # Close individual dimensions cannot bypass a substantially wrong aspect ratio.
        distorted=np.array([[0,0],[180,0],[180,170],[0,170]])
        self.assertFalse(rectangle_quality(distorted,[220,148])['valid'])
    def test_perspective_images_use_rectangular_stl_ratio_at_multiple_rotations(self):
        from copy import deepcopy
        mesh=deepcopy(self.mesh);mesh['size_mm']=[100,50,25]
        K=np.array(self.profile['intrinsics']['K']);D=np.array(self.profile['intrinsics']['D'])
        T=np.linalg.inv(np.array(self.profile['extrinsics']['base_from_camera']))
        for angle in (0,35,100):
            a=np.deg2rad(angle);R=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
            frame=np.full((720,1280,3),120,np.uint8)
            for fraction,color in ((1,240),(.4,120)):
                corners=np.array([[-50,-25],[50,-25],[50,25],[-50,25]])*fraction
                xy=corners@R.T+[230,-135];world=np.c_[xy,np.full(4,12.6)]
                camera=world@T[:3,:3].T+T[:3,3]
                uv,_=cv2.projectPoints(camera,np.zeros(3),np.zeros(3),K,D)
                cv2.fillConvexPoly(frame,np.rint(uv).astype(np.int32),(color,)*3)
            out=detect(frame,mesh,profile=self.profile)
            self.assertIsNotNone(out['selected'],(angle,out['status']))
            chosen=out['selected'];self.assertTrue(chosen['outer_shape']['valid'])
            self.assertEqual(chosen['symmetry_deg'],180)
            self.assertLess(abs((chosen['metric']['yaw_deg']-angle+90)%180-90),3)
            wrong=detect(frame,self.mesh,profile=self.profile);self.assertIsNone(wrong['selected'])
    def test_projected_axes_are_robot_xy_and_rotate_with_pose(self):
        for angle in (0,30,80):
            c={'metric':{'center_xy_mm':[230,-135],'yaw_deg':angle}}
            uv=projected_jig_axes(c,self.profile,12.6);xy=world_xy(uv,self.profile,12.6)
            a=np.deg2rad(angle)
            np.testing.assert_allclose(xy[1]-xy[0],[17*np.cos(a),17*np.sin(a)],atol=.002)
            np.testing.assert_allclose(xy[3]-xy[0],[-17*np.sin(a),17*np.cos(a)],atol=.002)
    def test_stl_rim_excludes_corner_posts_and_keeps_one_real_hole(self):
        self.assertEqual(self.mesh['size_mm'],[70,70,25]);self.assertEqual(self.mesh['rim_z_mm'],20)
        self.assertEqual(len(self.mesh['holes']),1)
        np.testing.assert_allclose(self.mesh['holes'][0]['center'],[.5,.5],atol=.005)
    def test_real_empty_pallet_matches_outer_box_not_desk_or_keyboard(self):
        image=cv2.imread(str(ROOT/'verification/pallet-current.jpg'));result=detect(image,self.mesh,profile=self.profile)
        self.assertEqual(result['status'],'shape_match');selected=result['selected']
        np.testing.assert_allclose(selected['center_px'],[727,424],atol=4)
        self.assertTrue(all(65<x<75 for x in selected['metric']['sides_mm']))
        self.assertFalse(result['verified_for_motion']);self.assertFalse(selected['metric']['verified'])
        self.assertFalse(result['edge_gap_repair'])
    def test_broken_outer_edges_recover_real_pallet_with_stl_and_hole_checks(self):
        image=cv2.imread(str(ROOT/'verification/edge-gap-repair/input.jpg'))
        result=detect(image,self.mesh,profile=self.profile)
        self.assertEqual(result['status'],'shape_match');self.assertTrue(result['edge_gap_repair'])
        chosen=result['selected'];np.testing.assert_allclose(chosen['center_px'],[589,436],atol=3)
        self.assertTrue(chosen['outer_shape']['valid']);self.assertGreater(min(chosen['hole_matches']),.55)
        # Weak illumination also clips a corner: recover detection without
        # claiming every inferred side is a precise 70 mm measurement.
        self.assertTrue(all(60<v<75 for v in chosen['metric']['sides_mm']))
        self.assertFalse(result['verified_for_motion'])
    def test_edge_repair_respects_roi_and_does_not_accept_wrong_stl_size(self):
        image=cv2.imread(str(ROOT/'verification/edge-gap-repair/input.jpg'))
        inside=detect(image,self.mesh,[.38,.47,.56,.77],self.profile)
        self.assertIsNotNone(inside['selected'])
        # A region cutting off half of this pallet cannot be repaired into a whole one.
        self.assertIsNone(detect(image,self.mesh,[.38,.47,.46,.77],self.profile)['selected'])
        wrong={**self.mesh,'size_mm':[110,70,25]}
        self.assertIsNone(detect(image,wrong,[.38,.47,.56,.77],self.profile)['selected'])
    def test_part_occluding_hole_must_not_be_accepted_as_empty_pallet(self):
        image=cv2.imread(str(ROOT/'verification/floor-contact/overhead.jpg'))
        result=detect(image,self.mesh,profile=self.profile)
        self.assertIsNone(result['selected'])
    def test_real_pallet_with_occluded_hole_is_accepted_after_square_continuity(self):
        image=cv2.imread(str(ROOT/'verification/floor-contact/overhead.jpg'))
        r=detect(image,self.mesh,profile=self.profile);latch=PoseLatch()
        for t in (0,.8,1.6,2.4):self.assertIsNone(latch.update(r,t)['selected'])
        out=latch.update(r,3.1)
        self.assertEqual(out['status'],'stable_candidate');self.assertTrue(out['selected']['outer_shape']['valid'])
        np.testing.assert_allclose(out['selected']['center_px'],[727,424],atol=4)
        self.assertFalse(out['selected']['shape_match']);self.assertFalse(out['verified_for_motion'])
    def test_roi_away_from_pallet_rejects_and_correct_roi_preserves_original_pixels(self):
        image=cv2.imread(str(ROOT/'verification/pallet-current.jpg'))
        self.assertIsNone(detect(image,self.mesh,[0,0,.4,.4],self.profile)['selected'])
        result=detect(image,self.mesh,[.45,.4,.8,.8],self.profile)
        np.testing.assert_allclose(result['selected']['center_px'],[727,424],atol=4)
    def test_blank_and_wrong_resolution_never_return_metric_pose(self):
        blank=np.full((720,1280,3),180,np.uint8);self.assertIsNone(detect(blank,self.mesh,profile=self.profile)['selected'])
        image=cv2.resize(cv2.imread(str(ROOT/'verification/pallet-current.jpg')),(640,360))
        self.assertIsNone(detect(image,self.mesh,profile=self.profile)['selected'])

if __name__=='__main__':unittest.main()
