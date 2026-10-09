import unittest,json,math
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.preview import overhead_view

class CameraPerspectiveTests(unittest.TestCase):
    def test_view_eye_and_direction_match_camera_pose(self):
        p=json.loads((ROOT/'assets/reference/camera.json').read_text());view=overhead_view(p);az,el=map(math.radians,view[:2]);forward=np.array([math.cos(az)*math.cos(el),math.sin(az)*math.cos(el),math.sin(el)])
        eye=np.array(view[3:])-forward*view[2];T=np.array(p['extrinsics']['base_from_camera'])
        np.testing.assert_allclose(eye,T[:3,3]/1000,atol=1e-8);np.testing.assert_allclose(forward,T[:3,2],atol=1e-8)
        self.assertAlmostEqual(view[5],-.0074)
