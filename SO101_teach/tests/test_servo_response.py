import unittest
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
from so101_teach.domain import JOINTS
from so101_teach.devices import ReadOnlyViolation
from so101_teach.servo_response import ResponseGate, REGISTERS, TARGETS, apply_response
from tests.fixtures import load_profile


class Bus:
    def __init__(self, cal, gate):
        self.rows = {n: dict.fromkeys(REGISTERS, 0) for n in JOINTS}
        for n, m in cal.motors.items():
            self.rows[n].update(P_Coefficient=16, D_Coefficient=32, Lock=1,
                                Homing_Offset=m.homing, Min_Position_Limit=m.low, Max_Position_Limit=m.high)
        self.ids = {n: m.id for n, m in cal.motors.items()}
        self.sent = []
        self.port = SimpleNamespace(writePort=lambda packet: self.sent.append(packet))
        gate.install(self.port, Counter())
        self.fail = None

    def read(self, reg, name, **kw):
        return self.rows[name][reg]

    def write(self, reg, name, value, **kw):
        address = {'P_Coefficient': 21, 'Lock': 55}[reg]
        data = [self.ids[name], 4, 3, address, value]
        self.port.writePort([255, 255, *data, (~sum(data)) & 255])
        self.rows[name][reg] = value
        if self.fail == (name, reg, value):
            self.fail = None
            raise OSError('setting accepted, ACK lost')


class ResponseTests(unittest.TestCase):
    def setUp(self):
        _, self.cal, _ = load_profile()
        self.gate = ResponseGate(self.cal)
        self.bus = Bus(self.cal, self.gate)

    def apply(self, save=lambda r: None):
        return apply_response(self.bus, self.cal, self.gate, 20, {}, save)

    def test_only_shoulder_elbow_p_change_and_second_apply_does_not_write(self):
        old = deepcopy(self.bus.rows)
        report = self.apply()
        for n in TARGETS:
            old[n]['P_Coefficient'] = 20
        self.assertEqual(self.bus.rows, old)
        self.assertEqual(report['state'], 'applied')
        self.assertEqual(len(self.bus.sent), 6)
        self.apply()
        self.assertEqual(len(self.bus.sent), 6)
        apply_response(self.bus, self.cal, self.gate, 16, {}, lambda r: None)
        self.assertTrue(all(self.bus.rows[n]['P_Coefficient'] == 16 for n in JOINTS))

    def test_torque_or_calibration_mismatch_rejected_before_any_write(self):
        for reg, value in [('Torque_Enable', 1), ('Homing_Offset', 9999)]:
            with self.subTest(reg=reg):
                old = self.bus.rows['gripper'][reg]
                self.bus.rows['gripper'][reg] = value
                with self.assertRaises(RuntimeError):
                    self.apply()
                self.assertFalse(self.bus.sent)
                self.bus.rows['gripper'][reg] = old

    def test_failed_second_joint_ack_rolls_back_both_and_locks(self):
        original = deepcopy(self.bus.rows)
        self.bus.fail = ('elbow_flex', 'P_Coefficient', 20)
        reports = []
        with self.assertRaises(OSError):
            self.apply(lambda r: reports.append(deepcopy(r)))
        self.assertEqual(self.bus.rows, original)
        self.assertEqual(reports[-1]['state'], 'rolled_back')

    def test_backup_failure_prevents_write(self):
        def fail(r):
            raise OSError('disk full')
        with self.assertRaises(OSError):
            self.apply(fail)
        self.assertFalse(self.bus.sent)

    def test_no_motion_gripper_or_unpermitted_writes(self):
        for n, reg, value in [('gripper', 'P_Coefficient', 20), ('shoulder_lift', 'Torque_Enable', 1),
                              ('elbow_flex', 'Goal_Position', 2047), ('elbow_flex', 'P_Coefficient', 32)]:
            with self.assertRaises(ReadOnlyViolation):
                self.gate.allow(n, reg, value)
        with self.assertRaises(ReadOnlyViolation):
            self.bus.write('P_Coefficient', 'elbow_flex', 20)
        self.gate.allow('elbow_flex', 'P_Coefficient', 20)
        with self.assertRaises(ReadOnlyViolation):
            self.bus.write('Lock', 'elbow_flex', 0)
        self.assertIsNone(self.gate.permit)

