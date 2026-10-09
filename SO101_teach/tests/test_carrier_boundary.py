import json,unittest
from pathlib import Path
import cv2,numpy as np
from so101_teach.vision import detect,project_plane
from so101_teach.height_reference import support_plane_profile,detection_plane_z

class CarrierBoundaryTests(unittest.TestCase):
    def test_outline_follows_user_confirmed_base_boundary_and_uses_same_pose(self):
        folder=Path(__file__).parent/'fixtures/vision-stationary';cfg=json.loads((folder/'fixture.json').read_text())
        j=cfg['jigs']['b5ebdb54807c441c8267c15d77713b6a'];m={**j['mesh'],'shape':j['shape'],'method':j['method']};p=support_plane_profile(cfg['profile'],j)
        out=detect(cv2.imread(str(folder/'carrier-boundary.png')),m,j['roi'],p);c=out['selected']
        self.assertTrue(c['observed_boundary_refined'])
        # Manually marked base-rim corners from the blue boundary confirmed by
        # the user. Raised orange/purple parts are not the carrier outline.
        reference=np.array([[1074,287],[1252,560],[1087,702],[891,395]])
        distances=np.linalg.norm(np.asarray(c['quad'])[:,None,:]-reference[None,:,:],axis=2)
        self.assertLess(float(distances.min(0).max()),3)
        self.assertLessEqual(abs(c['boundary_scale']-1),.03)
        self.assertLessEqual(c['boundary_corner_error_mm'],2)
        self.assertGreaterEqual(sorted(c['boundary_coverage'])[1],.85)
        self.assertEqual(c['metric']['dimension_source'],'observed_boundary_fit')
        np.testing.assert_allclose(c['center_px'],project_plane([c['metric']['center_xy_mm']],p,detection_plane_z(p,m))[0],atol=.001)
        self.assertFalse(out['verified_for_motion']);self.assertEqual(m['size_mm'],[220.,148.,6.])
