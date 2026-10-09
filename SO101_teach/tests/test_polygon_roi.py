from copy import deepcopy
from pathlib import Path
import json,tempfile,unittest
import cv2,numpy as np
from so101_teach.roi_geometry import roi_vertices,ordered_roi,roi_mask
from so101_teach.configuration import JigCatalog
from so101_teach.vision import detect

class PolygonROITests(unittest.TestCase):
    def test_four_corners_and_legacy_rectangle_roundtrip(self):
        points=ordered_roi([[.8,.8],[.1,.2],[.7,.1],[.2,.9]])
        self.assertEqual(np.asarray(points).shape,(4,2));self.assertEqual(roi_vertices([.1,.2,.8,.9]),[[.1,.2],[.8,.2],[.8,.9],[.1,.9]])
        with tempfile.TemporaryDirectory() as temp:
            c=JigCatalog(temp);c.save({**c.items['pallet'],'roi':points})
            self.assertEqual(JigCatalog(temp).items['pallet']['roi'],points)
    def test_crossed_duplicate_nonfinite_and_outside_rejected(self):
        for points in ([[.1,.1],[.8,.8],[.8,.1],[.1,.8]],[[.1,.1]]*4,[[float('nan'),0],[1,0],[1,1],[0,1]],[[0,0],[2,0],[1,1],[0,1]]):
            with self.assertRaises(ValueError):roi_vertices(points)
    def test_polygon_mask_excludes_bounding_box_corners(self):
        mask=roi_mask([[.5,.1],[.9,.5],[.5,.9],[.1,.5]],100,100)
        self.assertEqual(mask[50,50],255);self.assertEqual(mask[12,12],0);self.assertEqual(mask[50,80],255)
    def test_distorted_pallet_with_polygon_region_and_cut_corner(self):
        folder=Path(__file__).parent/'fixtures/vision-lens';cfg=json.loads((folder/'fixture.json').read_text());f=cv2.imread(str(folder/'input.jpg'));j=cfg['jigs']['pallet'];m={**j['mesh'],'shape':j['shape'],'method':j['method']}
        full=[[.165,.435],[.255,.34],[.30,.515],[.205,.60]]
        r=detect(f,m,full,cfg['profile']);self.assertIsNotNone(r['selected'])
        cut=[[.15,.34],[.30,.34],[.18,.60],[.15,.60]]
        self.assertIsNone(detect(f,m,cut,cfg['profile'])['selected'])
