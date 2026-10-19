"""Compatibility entry point for the original opt-in YOLO camera check.

The dual-runtime test replaces the historical desktop NCNN-only check.
"""
import os
import unittest
from tests.test_yolo_dual_live import DualYoloLiveTests

@unittest.skipUnless(os.environ.get('SO101_LIVE_YOLO')=='1','Explicit live camera YOLO verification')
class LiveYoloTests(unittest.TestCase):
    setUp=DualYoloLiveTests.setUp
    tearDown=DualYoloLiveTests.tearDown
    test_actual_camera_recognition_and_independent_tab=DualYoloLiveTests.test_live_models_thresholds_fps_and_capture
