import unittest,time,threading
from copy import deepcopy
from itertools import combinations
from unittest.mock import patch
import numpy as np
from tests.test_pose_hold import result
from so101_teach.jig_consensus import StableCandidate,dominant_group,agrees,checked_attempts
from so101_teach.vision import PoseLatch
from so101_teach.devices import CameraSession
from so101_teach.vision_service import MultiDetector

class ConsensusTests(unittest.TestCase):
    def test_every_new_frame_counts_at_minimum_and_maximum_duration(self):
        for seconds in (3,10):
            stable=StableCandidate(seconds)
            for i in range(seconds*10):
                out=stable.update(result(strong=True),i/10)
                self.assertIsNone(out['selected'])
            out=stable.finish(seconds)
            self.assertEqual(out['stable_candidate_samples'],seconds*10)
            self.assertEqual(out['adoption_inliers'],seconds*10)
            self.assertEqual(out['pose_measured_at'],seconds-.1)
            self.assertIsNotNone(out['selected']);self.assertIsNone(stable.finish(seconds))
    def test_slow_processing_gaps_do_not_reset_window_and_two_is_minimum(self):
        stable=StableCandidate(5)
        stable.update(result(100,strong=True),0);stable.update(result(102,strong=True),2.1)
        out=stable.finish(5);self.assertEqual(out['selected']['metric']['center_xy_mm'],[101,100]);self.assertEqual(out['stable_candidate_samples'],2)
        for count in (0,1):
            stable.clear(0)
            if count:stable.update(result(strong=True),1)
            out=stable.finish(5);self.assertIsNone(out['selected']);self.assertEqual(out['status'],'acquisition_failed');self.assertEqual(out['acquisition_issue'],'관측 부족')
    def test_repeat_timestamp_and_late_frame_cannot_fill_a_failed_window(self):
        stable=StableCandidate(3)
        for _ in range(5):stable.update(result(strong=True),0)
        out=stable.update(result(strong=True),3.1)
        self.assertIsNone(out['selected']);self.assertEqual(out['stable_candidate_samples'],1);self.assertEqual(out['acquisition_completed_attempts'],1)
        stable.update(result(strong=True),4.9);out=stable.finish(6.1)
        self.assertIsNotNone(out['selected']);self.assertEqual(out['stable_candidate_samples'],2);self.assertEqual(out['acquisition_completed_attempts'],2)
    def test_majority_median_rejects_movement_ties_and_unknown_heading(self):
        stable=StableCandidate()
        for i,x in enumerate([100,101,150,102,103]):stable.update(result(x,strong=True),i*.5)
        out=stable.finish(3);self.assertEqual(out['selected']['center_px'],[101.5,100]);self.assertEqual(out['adoption_rejected'],1)
        for positions in ([100,100,130,130],[100,100,100,130]):
            stable.clear()
            for i,x in enumerate(positions):stable.update(result(x,strong=True),i*.5)
            self.assertIsNone(stable.finish(3)['selected'])
        stable.clear();stable.update(result(strong=True),0);stable.update(result(strong=True),1)
        stable.update({'status':'orientation_unconfirmed','selected':None,'candidates':[]},2)
        self.assertIsNone(stable.finish(3)['selected'])
    def test_clique_matches_exhaustive_small_cases_without_chain_merging(self):
        rng=np.random.default_rng(17)
        for _ in range(25):
            candidates=[result(float(x),strong=True)['selected'] for x in rng.integers(95,112,9)]
            expected=0
            for n in range(len(candidates),len(candidates)//2,-1):
                if any(all(agrees(candidates[i],candidates[j]) for i,j in combinations(g,2)) for g in combinations(range(len(candidates)),n)):expected=n;break
            group=dominant_group(candidates);self.assertEqual(len(group),expected)
            self.assertTrue(all(agrees(a,b) for a,b in combinations(group,2)))
    def test_window_timer_keeps_original_measurement_and_capture_timestamps(self):
        latch=PoseLatch();latch.clear(0)
        latch.update(result(strong=True),.8);latch.update(result(strong=True),2.6)
        out=latch.poll(3);self.assertIsNotNone(out['selected']);self.assertEqual(out['pose_measured_at'],2.6)
        self.assertAlmostEqual(out['hold_remaining_s'],9.6)
        self.assertEqual(latch.poll(3.2)['pose_measured_at'],2.6)
        latch.freeze(True);self.assertEqual(latch.poll(100)['pose_measured_at'],2.6)
    def test_bounds_and_configuration_do_not_change_frozen_latch(self):
        for bad in (2.9,10.1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):StableCandidate(bad)
        for bad in (0,6,1.5,float('nan')):
            with self.assertRaises(ValueError):checked_attempts(bad)
        self.assertEqual(checked_attempts('5'),5)
        latch=PoseLatch();latch.freeze(True)
        with self.assertRaises(ValueError):latch.configure(10,acquisition_seconds=5)
        self.assertEqual(latch.stability.seconds,3)
    def test_poll_never_mixes_inflight_jig_or_different_frame_generation(self):
        from types import SimpleNamespace
        cat=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda k:{})
        detector=MultiDetector(cat,{});detector.refresh()
        with patch('so101_teach.vision_service.detect',return_value=result(strong=True)),patch('so101_teach.vision_service.time.monotonic',return_value=0):first=detector.process(np.zeros((20,20,3),np.uint8))
        with patch('so101_teach.vision_service.detect',return_value=result(120,strong=True)),patch('so101_teach.vision_service.time.monotonic',return_value=1):second=detector.process(np.zeros((20,20,3),np.uint8))
        self.assertEqual(detector.finish_observation(first,3),first)
        detector.clear();self.assertEqual(detector.finish_observation(second,4),second)

    def test_failed_windows_stop_exactly_at_one_or_five_attempts_until_explicit_clear(self):
        for limit in (1,5):
            stable=StableCandidate(3,limit);stable.clear(0)
            for attempt in range(1,limit+1):
                stable.update(result(strong=True),(attempt-1)*3+.5)
                out=stable.finish(attempt*3)
                self.assertIsNone(out['selected']);self.assertEqual(out['acquisition_completed_attempts'],attempt)
            self.assertTrue(out['acquisition_exhausted'])
            for t in (limit*3+1,limit*3+2,limit*3+3):self.assertEqual(stable.update(result(strong=True),t),out)
            stable.clear(limit*3+4);stable.update(result(strong=True),limit*3+5);stable.update(result(strong=True),limit*3+6)
            accepted=stable.finish(limit*3+7);self.assertIsNotNone(accepted['selected']);self.assertEqual(accepted['acquisition_completed_attempts'],1)
    def test_native_observation_finishes_window_without_changing_frame_age(self):
        from types import SimpleNamespace
        cat=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda k:{})
        d=MultiDetector(cat,{});frame=np.zeros((20,20,3),np.uint8)
        for t in (0,1,2):
            with patch('so101_teach.vision_service.detect',return_value=result(strong=True)),patch('so101_teach.vision_service.time.monotonic',return_value=t):data=d.process(frame)
        c=CameraSession(0,processor=d.process);c.running=True;c.observation=(frame,data,1.9)
        with patch('so101_teach.devices.time.monotonic',return_value=3):obs=c.observation
        self.assertIs(obs[0],frame);self.assertEqual(obs[2],1.9);self.assertIsNotNone(obs[1]['selected']);self.assertEqual(obs[1]['pose_measured_at'],2)
        self.assertEqual(c._observation[1],data)
