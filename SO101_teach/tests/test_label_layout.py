import unittest
from so101_teach.label_layout import LabelLayout

class LabelLayoutTests(unittest.TestCase):
    def test_labels_avoid_parts_borders_each_other_and_image_edges(self):
        layout=LabelLayout((720,1280),[[[400,400],[600,600]],[[800,280],[1150,650]]])
        for point in ((450,450),(450,450),(850,500),(1200,680)):
            previous=list(layout.blocked+layout.labels);position=layout.place(180,32,point)
            self.assertIsNotNone(position);x,y=position
            self.assertGreaterEqual(x,4);self.assertLessEqual(x+180,1276)
            self.assertGreaterEqual(y,4);self.assertLessEqual(y+32,716)
            self.assertFalse(any(x<x1 and x+180>x0 and y<y1 and y+32>y0 for x0,y0,x1,y1 in previous))
    def test_no_room_never_places_text_over_parts(self):
        layout=LabelLayout((100,100),[[[0,0],[100,100]]])
        self.assertIsNone(layout.place(50,20,(50,50)))
