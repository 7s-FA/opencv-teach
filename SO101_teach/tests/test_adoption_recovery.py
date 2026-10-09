import unittest
from copy import deepcopy
from tests.test_pose_hold import result
from so101_teach.vision import PoseLatch

class AdoptionRecoveryTests(unittest.TestCase):
    def test_sparse_strong_votes_still_wait_for_fixed_window(self):
        latch=PoseLatch(acquisition_seconds=3.)
        for t in (0,.8,1.6,2.4):self.assertIsNone(latch.update(result(strong=True),t)['selected'])
        self.assertIsNotNone(latch.update(result(strong=True),3.1)['selected'])
    def test_missing_slot_does_not_replace_a_new_observation(self):
        latch=PoseLatch(acquisition_seconds=3.)
        missing={'selected':None,'candidates':[],'status':'not_found'}
        for t in (0,.8,1.6):latch.update(result(strong=True),t)
        self.assertIsNone(latch.update(missing,2.4)['selected'])
        latch.update(result(101,strong=True),2.9);out=latch.poll(3)
        self.assertIsNotNone(out['selected']);self.assertEqual(out['stable_candidate_samples'],4)
    def test_ambiguity_calibration_and_stalled_stream_restart(self):
        for status in ('ambiguous','calibration_mismatch','error'):
            latch=PoseLatch()
            for t in (0,.8,1.6):latch.update(result(strong=True),t)
            latch.update({'selected':None,'candidates':[],'status':status},2.4)
            self.assertIsNone(latch.update(result(strong=True),3.1)['selected'])
    def test_weak_candidate_still_needs_three_independent_votes(self):
        latch=PoseLatch(acquisition_seconds=3.)
        for t in (0,1,2):self.assertIsNone(latch.update(result(),t)['selected'])
        self.assertIsNotNone(latch.update(result(),3.1)['selected'])
    def test_unique_shape_match_is_not_rejected_by_unverified_alternative(self):
        r=result(strong=True);weak=deepcopy(result(160)['candidates'][0]);weak['score']=r['selected']['score']-.01;r['candidates'].append(weak)
        latch=PoseLatch(acquisition_seconds=3.)
        for t in (0,.8,1.6,2.4):latch.update(r,t)
        self.assertIsNotNone(latch.update(r,3.1)['selected'])
