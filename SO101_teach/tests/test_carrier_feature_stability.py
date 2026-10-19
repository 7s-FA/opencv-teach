from pathlib import Path
from copy import deepcopy
import json,unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.model_regions import regions_mask
from so101_teach.mask_pose import detect_mask_seeded
from so101_teach.vision import detect
from so101_teach.two_frame_gate import quality
from so101_teach.grid_model import grid_hypotheses


class CarrierFeatureStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(__file__).parent/'fixtures/carrier-feature-stability'
        config=json.loads((cls.folder/'fixture.json').read_text());cls.profile=config['profile']
        catalog=JigCatalog(ROOT/'data',profile=cls.profile);catalog.items=config['jigs']
        for item in catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        item=catalog.items[config['key']];cls.mesh=catalog.mesh(config['key']);cls.roi=item['roi']
        cls.plane=support_plane_profile(cls.profile,item);cls.cases={c['trial']:c for c in config['cases']}

    def run_case(self,trial):
        case=self.cases[trial];frame=cv2.imread(str(self.folder/case['image']))
        if trial==2:
            a=np.load(self.folder/'mask-2.npz')
            rows=[{'mask':m,'box':b.tolist(),'class_id':int(c),'confidence':float(s)} for m,b,c,s in zip(a['masks'],a['boxes'],a['classes'],a['scores'])]
            return detect_mask_seeded(frame,self.mesh,self.roi,self.plane,rows,self.profile['jig_detection'])
        return detect(frame,self.mesh,self.roi,self.plane,search_mask=regions_mask(frame,case['regions']))

    def test_circle_chooses_supported_shift_without_relaxing_early_quality(self):
        c=self.run_case(2)['selected']
        self.assertEqual(c['orientation_method'],'circular_opening')
        self.assertGreaterEqual(c['orientation_scores'][0]['supported_sectors'],6)
        self.assertLessEqual(c['orientation_scores'][0]['cost'],.25)
        self.assertIsNone(quality(c))
        np.testing.assert_allclose(c['metric']['center_xy_mm'],self.cases[2]['reference']['center_xy_mm'],atol=.001)

    def test_fragmented_grid_recovers_from_current_image_without_moving_pose(self):
        for trial in (12,19):
            with self.subTest(trial=trial):
                c=self.run_case(trial)['selected']
                self.assertTrue(c['grid_contrast_recovery']);self.assertTrue(c['grid_verified'])
                self.assertLessEqual(c['grid_alignment_error_mm'],5.);self.assertIsNone(quality(c))
                np.testing.assert_allclose(c['metric']['center_xy_mm'],self.cases[trial]['reference']['center_xy_mm'],atol=.001)
                self.assertAlmostEqual(c['metric']['yaw_deg'],self.cases[trial]['reference']['yaw_deg'],places=3)

    def test_supported_alternatives_resolve_previous_alignment_failure(self):
        c=self.run_case(17)['selected']
        self.assertTrue(c['grid_verified']);self.assertLessEqual(c['grid_alignment_error_mm'],5.)
        self.assertIsNone(quality(c))

    def test_contrast_enhancement_cannot_invent_grid_on_flat_image(self):
        frame=np.full((720,1280,3),128,np.uint8);mask=np.full((720,1280),255,np.uint8)
        for refit in (False,True):
            self.assertEqual(grid_hypotheses(frame,self.mesh,mask,self.plane,visible=mask,enhanced=True,refine_lines=refit),[])
