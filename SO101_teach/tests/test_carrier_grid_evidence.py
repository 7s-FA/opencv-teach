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


class CarrierGridEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=Path(__file__).parent/'fixtures/carrier-grid-evidence'
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

    def test_recorded_failures_keep_pose_and_existing_quality_limits(self):
        for trial in (2,18):
            c=self.run_case(trial)['selected'];self.assertIsNotNone(c)
            self.assertIsNone(quality(c));self.assertLessEqual(c['grid_alignment_error_mm'],5.)
            np.testing.assert_allclose(c['metric']['center_xy_mm'],self.cases[trial]['reference']['center_xy_mm'],atol=.1)
            self.assertLess(abs(c['metric']['yaw_deg']-self.cases[trial]['reference']['yaw_deg']),.1)

    def test_two_observed_fragments_need_both_junctions_and_current_pixel_support(self):
        from tests.test_grid_detection import GridDetectionTests as Fixture
        from so101_teach.frame_evidence import RegionEvidence
        from so101_teach.vision import project_plane
        Fixture.setUpClass();helper=Fixture();frame,profile=helper.synthetic();mesh=helper.mesh
        points=grid.grid_layout(mesh)['points'];y=float(points[0,1]);allowed=np.full(frame.shape[:2],255,np.uint8)
        # Leave a genuine unobserved middle gap; each junction still has its
        # own measured cross fragment and the required local support.
        cut=np.rint(project_plane([[-12,y],[12,y]],profile,0)).astype(int)
        cv2.line(frame,tuple(cut[0]),tuple(cut[1]),(60,60,60),6)
        def line(a,b):
            xy=np.array([a,b],float);v=xy[1]-xy[0];length=float(np.linalg.norm(v));direction=v/length
            normal=np.array([-direction[1],direction[0]]);center=xy.mean(0)
            return {'xy':xy,'length':length,'direction':direction,'normal':normal,'center':center,'offset':float(normal@center)}
        parallels=[line([x,-50],[x,50]) for x in points[:,0]]
        left=line([-100,y],[-12,y]);right=line([12,y],[100,y])
        lines=[*parallels,left,right];candidate={'metric':{'center_xy_mm':[0,0],'yaw_deg':0},'boundary_scale':1.}
        evidence=RegionEvidence(frame,allowed)
        obs=grid.verify_grid_fragments(candidate,lines,mesh,allowed,profile,allowed,evidence)
        self.assertIsNotNone(grid.matching_grid(candidate,obs,mesh))
        missing=grid.verify_grid_fragments(candidate,[*parallels,left],mesh,allowed,profile,allowed,evidence)
        self.assertIsNone(grid.matching_grid(candidate,missing,mesh))
        wrong=grid.verify_grid_fragments(candidate,[*parallels,left,line([12,y+4],[100,y+4])],mesh,allowed,profile,allowed,evidence)
        self.assertIsNone(grid.matching_grid(candidate,wrong,mesh))
        blank=np.full_like(frame,60);stale=grid.verify_grid_fragments(candidate,lines,mesh,allowed,profile,allowed,RegionEvidence(blank,allowed))
        self.assertEqual(stale,[])
