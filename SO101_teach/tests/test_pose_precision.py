import json,unittest
from pathlib import Path
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.vision import detect,project_plane
from so101_teach.height_reference import support_plane_profile,detection_plane_z

class PrecisionTests(unittest.TestCase):
    def test_real_stationary_carrier_reduces_grid_jump_without_changing_size_or_plane(self):
        root=Path(__file__).parent/'fixtures/vision-precision';config=json.loads((root/'fixture.json').read_text())
        j=config['jigs']['b5ebdb54807c441c8267c15d77713b6a'];mesh={**j['mesh'],'shape':j['shape'],'method':j['method']};profile=support_plane_profile(config['profile'],j)
        old=[];new=[]
        for i in (0,6):
            frame=cv2.imread(str(root/f'frame{i}.jpg'))
            with patch('so101_teach.pose_refinement.refine',side_effect=lambda *args,**kwargs:args[-1]):a=detect(frame,mesh,j['roi'],profile)['selected']
            b=detect(frame,mesh,j['roi'],profile)['selected'];self.assertTrue(b['subpixel_refined'])
            self.assertLess(b['refinement_objective_after'],b['refinement_objective_before'])
            np.testing.assert_allclose(sorted(b['metric']['sides_mm']),sorted(a['metric']['sides_mm']),atol=.001)
            np.testing.assert_allclose(b['center_px'],project_plane([b['metric']['center_xy_mm']],profile,detection_plane_z(profile,mesh))[0],atol=.001)
            old.append(a['metric']['center_xy_mm']);new.append(b['metric']['center_xy_mm'])
        self.assertLess(np.linalg.norm(np.diff(new,axis=0)),np.linalg.norm(np.diff(old,axis=0))*.6)
    def test_pallet_refinement_keeps_independent_opening_and_outer_evidence(self):
        root=Path(__file__).parent/'fixtures/vision-precision';cfg=json.loads((root/'fixture.json').read_text());j=cfg['jigs']['pallet']
        mesh={**j['mesh'],'shape':j['shape'],'method':j['method']};p=support_plane_profile(cfg['profile'],j)
        out=detect(cv2.imread(str(root/'frame3.jpg')),mesh,j['roi'],p);c=out['selected']
        self.assertTrue(c['subpixel_refined']);self.assertGreater(c['hole_matches'][0],.55)
        self.assertGreaterEqual(sorted(c['edge_support'][4:])[1],.55);self.assertGreaterEqual(min(c['edge_support'][:4]),.25)
        self.assertFalse(out['verified_for_motion']);self.assertLessEqual(max(abs(np.array(c['refinement_delta_mm_deg'])/[2,2,1])),1)
