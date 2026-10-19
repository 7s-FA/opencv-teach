from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import json,unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.model_regions import regions_mask
from so101_teach.mask_pose import detect_mask_seeded
from so101_teach.vision import detect
from so101_teach.two_frame_gate import quality
import so101_teach.grid_model as grid


class CarrierGridAlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(__file__).parent/'fixtures/carrier-grid-alignment'
        config=json.loads((cls.folder/'fixture.json').read_text());cls.profile=config['profile']
        catalog=JigCatalog(ROOT/'data',profile=cls.profile);catalog.items=config['jigs']
        for item in catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        item=catalog.items[config['key']];cls.mesh=catalog.mesh(config['key']);cls.roi=item['roi']
        cls.plane=support_plane_profile(cls.profile,item);cls.cases={c['trial']:c for c in config['cases']}

    def run_case(self,trial):
        case=self.cases[trial];frame=cv2.imread(str(self.folder/case['image']))
        if trial==17:
            a=np.load(self.folder/'mask-17.npz')
            rows=[{'mask':m,'box':b.tolist(),'class_id':int(c),'confidence':float(s)} for m,b,c,s in zip(a['masks'],a['boxes'],a['classes'],a['scores'])]
            return detect_mask_seeded(frame,self.mesh,self.roi,self.plane,rows,self.profile['jig_detection'])
        return detect(frame,self.mesh,self.roi,self.plane,search_mask=regions_mask(frame,case['regions']))

    def test_all_four_failures_use_supported_crossings_with_same_tolerances(self):
        for trial in (4,7,8,17):
            with self.subTest(trial=trial):
                c=self.run_case(trial)['selected'];self.assertIsNotNone(c)
                self.assertLessEqual(c['grid_alignment_error_mm'],5.);self.assertIsNone(quality(c))
                support=np.asarray(c['grid_arm_support'])
                self.assertTrue(np.all(np.count_nonzero(support>=.5,axis=1)>=3))
                np.testing.assert_allclose(c['metric']['center_xy_mm'],self.cases[trial]['reference']['center_xy_mm'],atol=.1)
                self.assertLess(abs(c['metric']['yaw_deg']-self.cases[trial]['reference']['yaw_deg']),.1)
                # Better candidate selection never changes the acceptance limit.
                bad=deepcopy(c);bad['grid_alignment_error_mm']=5.001
                self.assertEqual(quality(bad),'carrier_alignment')

    def test_seed_shortlist_stays_bounded_but_does_not_discard_final_evidence(self):
        original=grid.grid_hypotheses;calls=[]
        def capture(*args,**kwargs):
            hints=original(*args,**kwargs)
            calls.append((deepcopy(hints),deepcopy(kwargs.get('observations',[]))))
            return hints
        with patch.object(grid,'grid_hypotheses',side_effect=capture):c=self.run_case(4)['selected']
        seeds,observations=calls[0]
        self.assertLessEqual(len(seeds),8);self.assertGreater(len(observations),len(seeds))
        self.assertGreater(grid.matching_grid(c,seeds,self.mesh)[0],5.)
        self.assertLess(grid.matching_grid(c,observations,self.mesh)[0],2.)
        chosen=deepcopy(grid.matching_grid(c,observations,self.mesh)[1])
        chosen['points_world']+=np.array([20.,0.])
        self.assertIsNone(grid.matching_grid(c,[chosen],self.mesh))
