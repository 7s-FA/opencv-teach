import unittest
from unittest.mock import patch
from tests import test_motion as fixtures
from so101_teach.domain import JOINTS

class SpeedReuseTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    def test_same_speed_is_read_back_without_another_write(self):
        self.s.configure_motion_speed(self.bus);first=list(self.writes)
        with patch.object(self.bus,'read',wraps=self.bus.read) as read:self.s.configure_motion_speed(self.bus)
        self.assertEqual(self.writes,first);self.assertEqual(read.call_count,12)
        self.assertEqual([c.args[0] for c in read.call_args_list],['Phase','Goal_Velocity']*6)
    def test_external_change_and_phase_mode_are_not_hidden_by_previous_settings(self):
        self.s.configure_motion_speed(self.bus);self.writes.clear()
        self.values[JOINTS[0]]['Goal_Velocity']=0;self.s.configure_motion_speed(self.bus)
        self.assertEqual(len(self.writes),1)
        self.writes.clear()
        for name in JOINTS:self.values[name]['Phase']=12
        self.s.configure_motion_speed(self.bus)
        self.assertEqual(len(self.writes),6);self.assertTrue(all(value==450 for _,_,value in self.writes))
    def test_read_failure_never_uses_last_success_as_evidence(self):
        self.s.configure_motion_speed(self.bus)
        with patch.object(self.bus,'read',side_effect=OSError('offline')):
            with self.assertRaisesRegex(OSError,'offline'):self.s.configure_motion_speed(self.bus)
    def test_failed_write_verification_still_blocks_move(self):
        with patch.object(self.bus,'write',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'속도 확인 실패'):self.s.configure_motion_speed(self.bus)
