from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np
from tests.test_two_frame_gate import MESH,SETTINGS,observation
from so101_teach.vision import PoseLatch
from so101_teach.vision_service import MultiDetector
from so101_teach.two_frame_gate import carrier_observation_reason


class CarrierThreeFrameTests(unittest.TestCase):
    def detector(self):
        catalog=SimpleNamespace(items={'carrier':{}},revision=0,mesh=lambda _:deepcopy(MESH))
        d=MultiDetector(catalog,{'jig_detection':deepcopy(SETTINGS)},active='carrier',default_latch=PoseLatch(10,4,5))
        d.region_model=Mock();d.region_model.predict.return_value=[]
        self.addCleanup(d.close)
        return d

    def run_frames(self,rows=None,ids=(1,2,3)):
        d=self.detector();clock=SimpleNamespace(now=10.)
        rows=rows or [observation(True),observation(),observation()]
        detect=Mock(side_effect=rows[1:]);outputs=[]
        with patch('so101_teach.vision_service.time',SimpleNamespace(monotonic=lambda:clock.now)),patch('so101_teach.vision_service.detect_mask_seeded',return_value=rows[0]),patch('so101_teach.vision_service.detect',detect):
            for index,value in enumerate(ids):
                clock.now=10.+index*.5
                frame=np.zeros((720,1280,3),np.uint8);frame[0,0]=value
                outputs.append(d.process(frame))
        return d,outputs,detect

    def test_three_distinct_cv_observations_adopt_without_same_image_recheck(self):
        d,out,cv=self.run_frames()
        self.assertTrue(all(r['selected'] is None for r in out[:2]))
        self.assertEqual(out[2]['adoption_method'],'carrier_three_frame_gate')
        self.assertEqual(out[2]['adoption_inliers'],3);self.assertEqual(cv.call_count,2)
        self.assertFalse(out[2]['verified_for_motion']);self.assertEqual(out[2]['pose_measured_at'],11.)
        d.freeze(True);self.assertEqual(d.finish_observation(out[2],40.)['pose_measured_at'],11.)
        d.freeze(False);self.assertEqual(d.latches['carrier'].stability.carrier_early_ids,[])

    def test_duplicate_or_weak_first_three_fall_back_to_identical_window(self):
        weak=observation();weak['selected']['grid_verified']=False
        for rows,ids in ((None,(1,1,3)),([observation(True),weak,observation()],(1,2,3))):
            d,out,_=self.run_frames(rows,ids)
            self.assertIsNone(out[-1]['selected'])
            baseline=PoseLatch(10,4,5);baseline.clear(10.)
            for i,r in enumerate(out):
                raw=deepcopy(r['live_by_jig']['carrier']);raw.pop('carrier_observation_id',None)
                baseline.update(raw,10.+i*.5)
            final=d.finish_observation(out[-1],14.)
            self.assertEqual(final['selected'],baseline.poll(14.)['selected'])
            self.assertNotIn('adoption_method',final)

    def test_metric_disagreement_blocks_early_and_does_not_choose_later_triple(self):
        rows=[observation(True)]+[observation() for _ in range(4)]
        rows[1]['selected']['metric']['center_xy_mm'][0]+=.6
        d,out,_=self.run_frames(rows,(1,2,3,4,5))
        self.assertTrue(all(r['selected'] is None for r in out))
        self.assertIsNotNone(d.finish_observation(out[-1],14.)['selected'])

    def test_heading_wrap_uses_median_and_half_turn_is_rejected(self):
        rows=[observation(True),observation(),observation()]
        for raw,yaw in zip(rows,(359.9,0.,.1)):raw['selected']['metric']['yaw_deg']=yaw
        _,out,_=self.run_frames(rows)
        self.assertAlmostEqual(out[-1]['selected']['metric']['yaw_deg'],0.)
        rows[1]['selected']['metric']['yaw_deg']=180.
        _,out,_=self.run_frames(rows);self.assertIsNone(out[-1]['selected'])

    def test_unknown_model_partial_multiple_and_unverified_evidence_are_ineligible(self):
        changes=[lambda r:r.update(model_identity='other'),lambda r:r.update(pose_held=True),
                 lambda r:r['candidates'].append(deepcopy(r['selected'])),
                 lambda r:r['selected'].update(partial_visible=True),
                 lambda r:r['selected'].update(boundary_coverage=[.46,1.,1.,1.]),
                 lambda r:r['selected'].update(grid_alignment_error_mm=5.1),
                 lambda r:r['selected'].update(orientation_verified=False),
                 lambda r:r['selected'].update(observed_boundary_refined=False),
                 lambda r:r['selected']['metric'].update(yaw_deg=float('nan'))]
        for change in changes:
            raw=observation();change(raw)
            self.assertIsNotNone(carrier_observation_reason(raw,MESH,(720,1280,3)))

    def test_new_window_can_use_three_tracking_frames_and_clear_drops_votes(self):
        latch=PoseLatch(10,4,5);latch.clear(0.)
        for t in (.5,1.):
            raw=observation();raw['carrier_observation_id']=str(t);latch.update(raw,t)
        latch.update({'selected':None,'candidates':[],'status':'orientation_unconfirmed'},2.)
        self.assertIsNone(latch.poll(4.)['selected'])
        for t in (4.2,4.5,4.8):
            raw=observation();raw['carrier_observation_id']=str(t);out=latch.update(raw,t)
        self.assertEqual(out['adoption_method'],'carrier_three_frame_gate')
        self.assertEqual(out['acquisition_completed_attempts'],2)
        latch.clear(5.);self.assertEqual(latch.stability.carrier_early_ids,[])
