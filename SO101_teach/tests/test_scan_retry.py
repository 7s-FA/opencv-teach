import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from so101_teach.devices import ReadOnlySession
from so101_teach.domain import JOINTS
from tests.fixtures import load_profile
class ScanRetryTests(unittest.TestCase):
    def setUp(self):
        _,cal,ref=load_profile();self.clock=100.;self.calls=0;self.durations=[.01]*6
        self.s=ReadOnlySession('fake',cal);self.s.stop.wait=lambda dt:setattr(self,'clock',self.clock+dt)
        self.patch=patch('so101_teach.devices.time.monotonic',lambda:self.clock);self.patch.start();self.addCleanup(self.patch.stop)
        def read(port,mid,address,length):
            i=self.calls;self.calls+=1;self.clock+=self.durations[i] if i<len(self.durations) else .01
            data=bytearray(31);data[16:18]=(2047+i//6).to_bytes(2,'little');data[22]=123;data[23]=35
            return data,0,0
        self.bus=SimpleNamespace(port_handler=Mock(),packet_handler=SimpleNamespace(readTxRx=read))
    def test_one_slow_scan_is_discarded_and_new_positions_used(self):
        self.durations=[.1]*6+[.01]*6;snap=self.s.collect_snapshot(self.bus,True)
        self.assertEqual(set(snap.ticks.values()),{2048});self.assertTrue(snap.fresh());self.assertEqual(self.calls,12)
        self.assertEqual(self.s.report['scan_recoveries'],1);self.assertIsNone(self.s.latest)
    def test_persistent_slow_collection_is_bounded(self):
        self.durations=[.1]*30
        with self.assertRaisesRegex(ConnectionError,'1.5초'):self.s.collect_snapshot(self.bus,True)
        self.assertEqual(self.calls,18);self.assertIsNone(self.s.latest)
    def test_no_timestamp_refresh_for_old_snapshot(self):
        self.durations=[.1]*6+[.01]*6;snap=self.s.collect_snapshot(self.bus,False)
        self.assertGreater(snap.monotonic,100.6);self.assertFalse(snap.calibration_matches)
    def test_fast_scan_not_retried(self):
        self.assertTrue(self.s.collect_snapshot(self.bus,True).fresh());self.assertEqual(self.calls,6)
    def test_cancelled_scan_does_not_publish_partial_values(self):
        original=self.bus.packet_handler.readTxRx
        def read(*args):
            value=original(*args);self.s.close();return value
        self.bus.packet_handler.readTxRx=read
        self.assertIsNone(self.s.collect_snapshot(self.bus,True));self.assertEqual(self.calls,1)
    def test_servo_alarm_is_not_hidden_by_retry(self):
        self.bus.packet_handler.readTxRx=Mock(return_value=(bytearray(31),0,4))
        with self.assertRaises(RuntimeError):self.s.collect_snapshot(self.bus,True)
        self.bus.packet_handler.readTxRx.assert_called_once()
