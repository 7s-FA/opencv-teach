import unittest
from unittest.mock import patch
import numpy as np
from so101_teach.inspection_overlay import draw_label

class InspectionLabelPositionTests(unittest.TestCase):
    def test_labels_keep_same_offset_above_each_part_without_other_marks(self):
        frame=np.zeros((100,100,3),np.uint8);badge=np.full((10,20,3),177,np.uint8)
        with patch('so101_teach.inspection_overlay.label_image',return_value=badge):
            draw_label(frame,'A 상단',(30,40),above=True)
            draw_label(frame,'B 상단',(70,60),above=True)
        expected=np.zeros_like(frame);expected[30:40,20:40]=badge;expected[50:60,60:80]=badge
        np.testing.assert_array_equal(frame,expected)
    def test_near_image_edges_stays_inside_original_frame(self):
        frame=np.zeros((100,100,3),np.uint8);badge=np.full((10,20,3),177,np.uint8)
        with patch('so101_teach.inspection_overlay.label_image',return_value=badge):
            draw_label(frame,'A 상단',(-1,2),above=True)
            draw_label(frame,'B 상단',(101,102),above=True)
        self.assertEqual(frame.shape,(100,100,3))
        np.testing.assert_array_equal(frame[:10,:20],badge);np.testing.assert_array_equal(frame[90:,80:],badge)

    def test_above_preference_never_covers_a_protected_part(self):
        from so101_teach.label_layout import LabelLayout
        frame=np.zeros((100,100,3),np.uint8);badge=np.full((10,20,3),177,np.uint8)
        layout=LabelLayout(frame.shape,[[[20,20],[50,50]]])
        with patch('so101_teach.inspection_overlay.label_image',return_value=badge):
            draw_label(frame,'A 상단',(30,40),above=True,layout=layout)
        self.assertTrue(frame.any());self.assertFalse(frame[14:56,14:56].any())
    def test_no_free_space_omits_badge_instead_of_covering_a_part(self):
        from so101_teach.label_layout import LabelLayout
        frame=np.zeros((100,100,3),np.uint8);layout=LabelLayout(frame.shape,[[[0,0],[99,99]]])
        with patch('so101_teach.inspection_overlay.label_image',return_value=np.ones((10,20,3),np.uint8)):
            draw_label(frame,'A 상단',(30,40),above=True,layout=layout)
        self.assertFalse(frame.any())
