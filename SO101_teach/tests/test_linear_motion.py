import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('linear_motion',Path(__file__).parents[1]/'tools/linear_motion.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class LinearMotionTests(unittest.TestCase):
    def setUp(self):
        self.now=100.; self.out=Mock(); self.save=Mock(); self.motion=m.LinearMotion(self.out,self.save,clock=lambda:self.now)
    def test_only_two_presets(self):
        for target in (0,42.7,101):
            with self.assertRaises(ValueError): self.motion.ensure(target)
        self.out.assert_not_called()
    def test_pause_and_resume_rejected_without_changing_output_or_deadline(self):
        self.motion.ensure(100.); self.now=103.
        for method in (self.motion.pause, self.motion.resume):
            with self.assertRaisesRegex(ValueError, 'PAUSE_UNSUPPORTED'): method()
        self.assertEqual(self.motion.phase, 'MOVING')
        self.assertEqual(self.motion.deadline - self.now, 5.)
        self.assertFalse(self.motion.status()['pause_supported'])
        self.assertNotIn('remaining_s', self.motion.status())
        self.out.assert_called_once_with(2000)
        self.now=108.; self.motion.tick()
        self.assertEqual(self.motion.phase, 'TIMED_COMPLETE')

    def test_duplicate_does_not_extend_move(self):
        self.motion.ensure(1.5); self.now=102.; self.motion.ensure(1.5)
        self.assertEqual(self.motion.deadline - self.now,6.); self.assertEqual(self.out.call_count,1)
    def test_completed_position_skips_but_unknown_history_does_not(self):
        restored={'phase':'TIMED_COMPLETE','target_mm':1.5,'remaining_s':0}
        driver=m.LinearMotion(self.out,self.save,restored,clock=lambda:self.now)
        driver.ensure(1.5); self.out.assert_not_called()
        driver=m.LinearMotion(self.out,self.save,{'target_mm':1.5,'phase':'MOVING'},clock=lambda:self.now)
        self.assertEqual(driver.phase,'UNKNOWN'); driver.ensure(1.5); self.out.assert_called_once()
    def test_cancel_does_not_restart_or_freeze_timed_move(self):
        self.motion.ensure(100.); self.now=103.; self.motion.cancel()
        self.assertEqual(self.motion.phase, 'MOVING')
        self.assertEqual(self.motion.deadline - self.now, 5.)
        self.out.assert_called_once_with(2000)
        with self.assertRaisesRegex(ValueError, 'BUSY'): self.motion.ensure(1.5)

    def test_ownership_loss_keeps_target_until_completion_and_blocks_new_owner(self):
        self.motion.claim('job'); self.motion.call({'op':'ensure','owner':'job','target_mm':100.})
        self.now=104.; self.motion.tick()
        self.assertEqual(self.motion.phase, 'MOVING'); self.assertIsNone(self.motion.owner)
        with self.assertRaisesRegex(ValueError,'BUSY'): self.motion.claim('other')
        self.out.assert_called_once_with(2000)
        self.now=108.; self.motion.tick(); self.motion.claim('other')
        self.assertEqual(self.motion.phase, 'TIMED_COMPLETE')

    def test_pwm_failure_is_never_done(self):
        self.out.side_effect=RuntimeError('GPIO')
        with self.assertRaises(RuntimeError): self.motion.ensure(100.)
        self.assertEqual(self.motion.phase,'ERROR')
    def test_legacy_paused_restore_is_unknown_and_never_auto_resumes(self):
        driver=m.LinearMotion(self.out,self.save,{'phase':'PAUSED','target_mm':1.5,'remaining_s':2.},clock=lambda:self.now)
        self.assertEqual(driver.phase, 'UNKNOWN')
        with self.assertRaisesRegex(ValueError, 'PAUSE_UNSUPPORTED'): driver.resume()
        self.out.assert_not_called()

    def test_release_does_not_change_motion_and_new_owner_waits(self):
        self.motion.claim('job'); self.motion.call({'op':'ensure','owner':'job','target_mm':100.})
        self.now=102.; self.motion.call({'op':'release','owner':'job'})
        self.assertEqual(self.motion.phase, 'MOVING')
        self.assertEqual(self.motion.deadline - self.now, 6.)
        with self.assertRaisesRegex(ValueError,'BUSY'): self.motion.claim('other')
        self.out.assert_called_once_with(2000)
