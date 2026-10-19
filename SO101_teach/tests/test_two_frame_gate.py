from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.two_frame_gate import MODEL_IDENTITY,CARRIER_SHA256,pair_reason,confirmation_reason
from so101_teach.vision_service import MultiDetector
from so101_teach.vision import PoseLatch

MESH={'shape':'rectangle','sha256':CARRIER_SHA256}
SETTINGS={'method':'model_roi','model':'n','confidence':.5,'model_identity':MODEL_IDENTITY}
def observation(seed=False):
    c={'center_px':[200.,200.],'quad':[[100,100],[300,100],[300,300],[100,300]],'area_px':40000.,'image_angle_deg':0.,
       'symmetry_deg':360,'metric':{'center_xy_mm':[200.,200.],'yaw_deg':0.,'dimension_source':'observed_boundary_fit'},
       'outer_shape':{'valid':True},'shape_match':True,'visible_fraction':1.,'observed_boundary_refined':True,
       'grid_verified':True,'orientation_verified':True,'boundary_coverage':[1.,1.,1.,1.],
       'boundary_corner_error_mm':.2,'grid_alignment_error_mm':1.,'orientation_hole_alignment_error_mm':1.,'boundary_scale':1.,
       'orientation_scores':[{'cost':.1,'supported_sectors':8},{'cost':.8,'supported_sectors':2}]}
    return {'selected':c,'candidates':[c],'status':'shape_match','mask_seed_refined':seed,'detection_method':'model_roi',
            'region_model':'n','model_identity':MODEL_IDENTITY,'tracking_state':'acquired' if seed else 'tracking'}

class TwoFrameGateTests(unittest.TestCase):
    def detector(self):
        catalog=SimpleNamespace(items={'carrier':{}},revision=0,mesh=lambda _:deepcopy(MESH))
        d=MultiDetector(catalog,{'jig_detection':deepcopy(SETTINGS)},active='carrier',default_latch=PoseLatch(10,4,5))
        d.region_model=Mock();d.region_model.predict.return_value=[]
        self.addCleanup(d.close)
        return d
    def test_weak_ambiguous_partial_motion_or_changed_context_rejected(self):
        changes=[lambda a,b:b.update(status='ambiguous'),lambda a,b:b['candidates'].append(deepcopy(b['selected'])),
                 lambda a,b:b['selected'].update(partial_visible=True),lambda a,b:b.update(tracking_local_retry=True),
                 lambda a,b:b.update(model_identity='x'*64),lambda a,b:b['selected']['metric'].update(yaw_deg=180),
                 lambda a,b:b['selected']['metric'].update(center_xy_mm=[201.,200.]),
                 lambda a,b:b['selected'].update(grid_alignment_error_mm=float('nan')),
                 lambda a,b:b['selected'].pop('orientation_scores'),lambda a,b:a.update(mask_seed_refined=False)]
        for change in changes:
            a,b=observation(True),observation();change(a,b)
            self.assertIsNotNone(pair_reason(a,b,MESH,(720,1280,3)))
        for mesh,shape in (({'shape':'ring'},(720,1280,3)),(MESH,(480,640,3)),({**MESH,'sha256':'other'},(720,1280,3))):
            self.assertIsNotNone(pair_reason(observation(True),observation(),mesh,shape))
    def test_independent_check_rejects_direction_and_disagreement(self):
        a,b=observation(True),observation()
        c=observation();c['selected']['metric']['center_xy_mm']=[201.,200.]
        self.assertEqual(confirmation_reason(a,b,c),'independent_disagreement')
        c=observation();c['selected']['orientation_verified']=False
        self.assertEqual(confirmation_reason(a,b,c),'independent_direction')
