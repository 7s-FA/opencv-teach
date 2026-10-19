"""Exact image evidence must survive the Pi allocation/caching changes."""
import unittest
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.frame_evidence import FrameEvidence,RegionEvidence
from so101_teach.grid_model import grid_hypotheses

class EvidenceOptimizationTests(unittest.TestCase):
    def test_white_lookup_is_exact_for_every_saturation_value_pair(self):
        hsv=np.zeros((256,256,3),np.uint8);hsv[:,:,1]=np.arange(256)[:,None];hsv[:,:,2]=np.arange(256)[None,:]
        evidence=FrameEvidence(None);evidence.cache['hsv']=hsv
        old=hsv.astype(float);expected=np.clip((old[:,:,2]-90)/90,0,1)*(1-np.clip((old[:,:,1]-40)/120,0,1))
        np.testing.assert_array_equal(evidence.white(),expected)
    def test_gradient_cache_matches_original_formula_and_is_frame_local(self):
        image=np.random.default_rng(2).integers(0,256,(90,140,3),dtype=np.uint8);source=FrameEvidence(image)
        gray=source.blurred(True);gx=cv2.Sobel(gray,cv2.CV_32F,1,0);gy=cv2.Sobel(gray,cv2.CV_32F,0,1)
        magnitude=np.hypot(gx,gy);gx/=np.maximum(magnitude,1);gy/=np.maximum(magnitude,1)
        actual=source.gradient_directions(True);np.testing.assert_array_equal(actual[0],gx);np.testing.assert_array_equal(actual[1],gy)
        self.assertIs(actual,source.gradient_directions(True));self.assertIsNot(actual,FrameEvidence(image).gradient_directions(True))
    def test_grid_refit_reuses_hough_but_not_a_different_visible_region(self):
        from tests.test_grid_detection import GridDetectionTests
        helper=GridDetectionTests();helper.setUpClass()
        # No segments is also a cached result; region changes must invalidate it.
        frame=np.zeros((80,120,3),np.uint8);allowed=np.full((80,120),255,np.uint8)
        region=RegionEvidence(frame,allowed);visible=allowed.copy()
        # Layout is enough to exercise Hough; no camera transform runs without segments.
        profile={'intrinsics':{'K':[]},'extrinsics':{'base_from_camera':[]}}
        with patch('so101_teach.grid_model.cv2.HoughLinesP',return_value=None) as hough:
            grid_hypotheses(frame,helper.mesh,allowed,profile,evidence=region,visible=visible)
            grid_hypotheses(frame,helper.mesh,allowed,profile,evidence=region,visible=visible,refine_lines=True)
            self.assertEqual(hough.call_count,1)
            grid_hypotheses(frame,helper.mesh,allowed,profile,evidence=region,visible=visible.copy())
            self.assertEqual(hough.call_count,1)
            changed=visible.copy();changed[0,0]=0
            grid_hypotheses(frame,helper.mesh,allowed,profile,evidence=region,visible=changed)
            self.assertEqual(hough.call_count,2)
