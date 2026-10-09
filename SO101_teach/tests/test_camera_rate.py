import unittest
from unittest.mock import Mock,patch
from so101_teach.devices import CameraSession
class CameraRateTests(unittest.TestCase):
    def test_capture_requests_ten_and_waits_remainder(self):
        import cv2
        c=CameraSession(0);cap=Mock();cap.isOpened.return_value=True;cap.read.return_value=(True,object())
        c.stop=Mock();c.stop.is_set.side_effect=[False,True]
        with patch.object(cv2,'VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',side_effect=[100.,100.02]):c.run()
        self.assertIsNone(c.error);cap.set.assert_any_call(cv2.CAP_PROP_FPS,10);self.assertAlmostEqual(c.stop.wait.call_args.args[0],.08);cap.release.assert_called_once()
    def test_slow_processing_does_not_sleep_or_catch_up(self):
        import cv2
        c=CameraSession(0);cap=Mock();cap.isOpened.return_value=True;cap.read.return_value=(True,object())
        c.stop=Mock();c.stop.is_set.side_effect=[False,True]
        with patch.object(cv2,'VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',side_effect=[100.,100.2]):c.run()
        self.assertIsNone(c.error);c.stop.wait.assert_called_once_with(0.)
