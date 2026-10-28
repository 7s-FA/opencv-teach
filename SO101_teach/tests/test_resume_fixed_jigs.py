"""Checkpoint retry retains carrier/pallet coordinates without opening detection."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from tests.test_checkpoint_resume import ResumeStateTests
from so101_teach.episode_faults import ResumeBlocked
from episode_resume import retained_jigs

class FixedJigResumeTests(unittest.TestCase):
    setUp=ResumeStateTests.setUp
    emit=ResumeStateTests.emit
    restart=ResumeStateTests.restart

    def prepare(self,key):
        r=self.runner;r.execution_bundle={'profile':{}}
        r.execution_steps=[{'id':'work','jig_id':key}]
        r.inspection_jigs={key:{'pose':[123.,45.,30.],'symmetry_deg':360,'stl_sha256':'original'}}
        r.inspection_batch=SimpleNamespace(recapture=Mock())
        r.linear_target=100.;r.prepare_camera=Mock();r.await_camera_ready=Mock()
        return r

    def test_carrier_and_pallet_retry_keep_original_basis_even_if_current_scene_cannot_detect(self):
        for key in ('carrier','pallet'):
            with self.subTest(key=key):
                r=self.prepare(key);original=deepcopy(r.inspection_jigs);same_object=r.inspection_jigs
                r.measure=Mock(side_effect=AssertionError('Current scene must not reacquire a jig'))
                for _ in range(2):
                    self.now[0]+=60 # Old acquisition TTL must not expire an execution basis.
                    r.recheck_checkpoint()
                r.measure.assert_not_called();self.assertIs(r.inspection_jigs,same_object)
                self.assertEqual(r.inspection_jigs,original);self.assertEqual(r.inspection_batch.recapture.call_count,2)
                calls=self.link.calls
                self.assertFalse(any(m in ('detect_clear','detect_freeze') for m,_ in calls))
                self.assertFalse(any(m=='camera_start' and a.get('processing_enabled') for m,a in calls))
                self.assertFalse(any(m=='command' for m,_ in calls))

    def test_missing_original_jig_blocks_without_replacement_detection(self):
        r=self.prepare('pallet');r.inspection_jigs={}
        with self.assertRaisesRegex(ResumeBlocked,'기존에 채택'):r.recheck_checkpoint()
        r.prepare_camera.assert_not_called();r.inspection_batch.recapture.assert_not_called()

    def test_linear_change_still_blocks_recheck(self):
        r=self.prepare('pallet');r.linear_target=1.5
        with self.assertRaisesRegex(ResumeBlocked,'리니어 위치'):r.recheck_checkpoint()
        r.inspection_batch.recapture.assert_not_called()

    def test_cached_basis_survives_preparation_retry_without_camera_detection(self):
        r=self.prepare('carrier');r.adopted_jigs=deepcopy(r.inspection_jigs);original=deepcopy(r.adopted_jigs)
        callback=Mock()
        with patch.object(self.link,'rpc',side_effect=AssertionError('No new detection RPC')):
            result=r.measure(r.execution_steps,{},on_progress=callback)
        callback.assert_called_once_with(original);self.assertEqual(result,original)
        result['carrier']['pose'][0]+=10
        self.assertEqual(r.adopted_jigs,original)

    def test_nonfinite_stored_basis_is_rejected(self):
        for value in (float('nan'),float('inf')):
            with self.subTest(value=value):
                r=self.prepare('pallet');r.inspection_jigs['pallet']['pose'][0]=value
                with self.assertRaises(ResumeBlocked):retained_jigs(r.execution_steps,r.inspection_jigs)

    def test_new_episode_discards_previous_arm_basis(self):
        r=self.prepare('carrier');r.adopted_jigs=deepcopy(r.inspection_jigs)
        def begin(*args):
            self.assertIsNone(r.adopted_jigs);self.assertEqual(r.inspection_jigs,{})
            raise RuntimeError('new episode boundary reached')
        with patch.object(r,'prepare_run_inspections',side_effect=begin):
            with self.assertRaisesRegex(RuntimeError,'boundary reached'):r._execute({}, {},400,1.5)
