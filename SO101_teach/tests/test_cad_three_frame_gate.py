"""First-three current AI/CAD votes; mismatches retain the full time window."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.jig_consensus import StableCandidate
from so101_teach.vision import PoseLatch
from so101_teach.vision_service import MultiDetector
from tests.test_pose_hold import result
from tests.test_cad_pallet import CadPalletTests


def cad_observation(identity,x=100,a=10):
    raw=result(x,a,strong=True);raw.update(pallet_pose_method='cad_silhouette_fit',cad_observation_id=identity)
    raw['selected']['cad_pose_validated']=True
    raw['selected']['metric']['dimension_source']='cad_silhouette_fit'
    return raw


class CadThreeFrameTests(unittest.TestCase):
    def test_three_distinct_agreeing_observations_adopt_their_median(self):
        latch=PoseLatch(10,4,5);latch.clear(0)
        for i,(at,x,a) in enumerate([(.55,101,89),(1.1,100,0),(1.65,102,1)]):
            out=latch.update(cad_observation(str(i),x,a),at)
            if i<2:self.assertIsNone(out['selected'])
        self.assertEqual(out['adoption_method'],'cad_three_frame_gate')
        self.assertEqual(out['adoption_inliers'],3);self.assertEqual(out['stable_candidate_samples'],3)
        self.assertEqual(out['acquisition_completed_at'],1.65)
        self.assertEqual(out['selected']['metric']['center_xy_mm'],[101.,100.])
        self.assertEqual(out['selected']['metric']['yaw_deg'],0.)
        self.assertFalse(out['verified_for_motion'])
        latch.freeze(True);held=latch.poll(30)
        self.assertEqual(held['selected'],out['selected']);self.assertEqual(held['pose_measured_at'],1.65)

    def test_first_three_mismatch_never_becomes_a_sliding_early_window(self):
        early=StableCandidate(4);old=StableCandidate(4);early.clear(0);old.clear(0)
        for i,(at,x) in enumerate([(.5,100),(1.,104),(1.5,108),(2.,108),(2.5,108),(3.,108)]):
            raw=cad_observation(str(i),x)
            actual=early.update(raw,at);baseline=deepcopy(raw);baseline.pop('cad_observation_id')
            old.update(baseline,at);self.assertIsNone(actual['selected'])
        # Adjacent pairs agreed, but 100 and 108 were not a matching pair.
        actual=early.finish(4);expected=old.finish(4)
        self.assertIsNotNone(actual['selected']);self.assertNotIn('adoption_method',actual)
        self.assertEqual(actual['selected'],expected['selected'])
        self.assertEqual(actual['adoption_inliers'],expected['adoption_inliers'])
        self.assertEqual(actual['acquisition_completed_at'],4.)

    def test_duplicate_image_disables_early_acceptance_for_this_window(self):
        stable=StableCandidate(4);stable.clear(0)
        for i,identity in enumerate(['same','second','same','fourth','fifth']):
            self.assertIsNone(stable.update(cad_observation(identity),.5*(i+1))['selected'])
        self.assertNotIn('adoption_method',stable.finish(4))

    def test_repeated_timestamp_is_not_an_additional_vote(self):
        stable=StableCandidate(4);stable.clear(0)
        self.assertIsNone(stable.update(cad_observation('first'),.5)['selected'])
        self.assertIsNone(stable.update(cad_observation('ignored'),.5)['selected'])
        self.assertIsNone(stable.update(cad_observation('second'),1.)['selected'])
        self.assertEqual(stable.update(cad_observation('third'),1.5)['adoption_inliers'],3)

    def test_non_cad_unvalidated_or_missing_current_ai_evidence_stays_on_full_window(self):
        for change in [lambda r:r.pop('cad_observation_id'),lambda r:r.pop('pallet_pose_method'),
                       lambda r:r['selected'].update(cad_pose_validated=False),
                       lambda r:r['selected']['metric'].update(dimension_source='observed_boundary_fit')]:
            stable=StableCandidate(4);stable.clear(0)
            for i in range(3):
                raw=cad_observation(str(i));change(raw)
                self.assertIsNone(stable.update(raw,.5*(i+1))['selected'])
            self.assertNotIn('adoption_method',stable.finish(4))

    def test_invalid_observation_breaks_the_first_three_evidence(self):
        stable=StableCandidate(4);stable.clear(0)
        stable.update(cad_observation('first'),.5)
        stable.update({'status':'ambiguous','selected':None,'candidates':[]},.8)
        for i in range(3):self.assertIsNone(stable.update(cad_observation(str(i)),1.+i*.5)['selected'])
        self.assertNotIn('adoption_method',stable.finish(4))

    def test_late_third_frame_cannot_relabel_a_finished_window(self):
        stable=StableCandidate(4);stable.clear(0)
        stable.update(cad_observation('first'),.5);stable.update(cad_observation('second'),1.)
        out=stable.update(cad_observation('late'),4.1)
        self.assertIsNotNone(out['selected']);self.assertEqual(out['adoption_inliers'],2)
        self.assertNotIn('adoption_method',out);self.assertEqual(out['acquisition_completed_at'],4.)
        self.assertEqual(len(stable.samples),1)

    def test_clear_and_new_attempt_require_three_new_observations(self):
        stable=StableCandidate(4);stable.clear(0)
        stable.update(cad_observation('old1'),.5);stable.update(cad_observation('old2'),1.)
        stable.clear(2.)
        for i in range(2):self.assertIsNone(stable.update(cad_observation(str(i)),2.5+i*.5)['selected'])
        self.assertEqual(stable.update(cad_observation('third'),3.5)['adoption_method'],'cad_three_frame_gate')
        stable.clear(4.)
        for i,x in enumerate([100,130,160]):stable.update(cad_observation(str(i),x),4.5+i*.5)
        self.assertIsNone(stable.finish(8.)['selected'])
        for i in range(3):out=stable.update(cad_observation('retry'+str(i)),8.5+i*.5)
        self.assertEqual(out['adoption_method'],'cad_three_frame_gate')
        self.assertEqual(out['acquisition_completed_attempts'],2)


class CurrentMaskGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):CadPalletTests.setUpClass()
    def detector(self):
        helper=CadPalletTests();case,frame,rows=helper.inputs('C001');jig=case['jigs']['pallet']
        catalog=SimpleNamespace(items={'pallet':{'roi':jig['roi'],'support_height_mm':0}},revision=0,mesh=lambda key:deepcopy(jig['mesh']))
        detector=MultiDetector(catalog,{**jig['profile'],'jig_detection':helper.settings},default_latch=PoseLatch(10,4,5))
        provider=Mock();provider.predict.return_value=rows;detector.region_model=provider
        self.addCleanup(detector.close)
        return detector,provider,frame
    def test_actual_cad_fits_require_three_fresh_inferences_and_no_extra_cv(self):
        detector,provider,frame=self.detector();ids=[]
        with patch('so101_teach.vision_service.detect') as legacy_cv:
            for i in range(3):
                current=frame.copy();current[0,0]=i
                with patch('so101_teach.vision_service.time.monotonic',return_value=.6*(i+1)):
                    out=detector.process(current)
                ids.append(out['live_by_jig']['pallet']['cad_observation_id'])
                if i<2:self.assertIsNone(out['selected'])
            legacy_cv.assert_not_called()
        self.assertEqual(len(set(ids)),3);self.assertEqual(provider.predict.call_count,3)
        self.assertEqual(out['adoption_method'],'cad_three_frame_gate')

    def test_same_camera_image_and_changed_context_do_not_reuse_three_votes(self):
        detector,provider,frame=self.detector()
        for i in range(3):
            with patch('so101_teach.vision_service.time.monotonic',return_value=.5*(i+1)):out=detector.process(frame.copy())
            self.assertIsNone(out['selected'])
        self.assertEqual(provider.predict.call_count,3)
        with patch('so101_teach.vision_service.time.monotonic',return_value=2.):detector.clear()
        for i in range(2):
            current=frame.copy();current[0,0]=i
            with patch('so101_teach.vision_service.time.monotonic',return_value=2.2+i*.5):detector.process(current)
        detector.catalog.revision+=1
        current=frame.copy();current[0,0]=10
        with patch('so101_teach.vision_service.time.monotonic',return_value=3.2):out=detector.process(current)
        self.assertIsNone(out['selected']);self.assertEqual(len(detector.latches['pallet'].stability.samples),1)
