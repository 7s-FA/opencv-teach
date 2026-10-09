import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_ui as fixtures
from so101_teach.telemetry import monitor_tick,monitor_load
from so101_teach.domain import atomic_json

class MonitorFormatTests(unittest.TestCase):
    def test_load_percent_ignores_direction_bit(self):
        for raw,text in [(0,'0 (0.0%)'),(1024,'1024 (0.0%)'),(50,'50 (4.9%)'),(1074,'1074 (4.9%)'),(1023,'1023 (100.0%)'),(2047,'2047 (100.0%)')]:self.assertEqual(monitor_load(raw),text)
        for bad in (None,-1,2048,'50'):self.assertEqual(monitor_load(bad),'—')
    def test_angle_uses_servo_calibration_or_nominal_scale(self):
        cal=SimpleNamespace(angle_mapping=SimpleNamespace(degrees=Mock(return_value=-90.)))
        self.assertEqual(monitor_tick(cal,'gripper',2047),'2047 (-90.0°)');cal.angle_mapping.degrees.assert_called_once_with('gripper',2047)
        self.assertEqual(monitor_tick(cal,'gripper',2047,verified=False),'2047 (영점 미확인)')
        self.assertEqual(monitor_tick(SimpleNamespace(angle_mapping=None),'shoulder_pan',3071),'3071 (90.0°)')

class DiagnosticScrollTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_both_panes_scroll_and_log_stays_read_only(self):
        for i in range(50):atomic_json(self.data/'diagnostics'/('long-diagnostic-file-name-'+str(i)+'.json'),{'lines':['x'*300]*100})
        self.app.settings.open_diagnostics();self.root.update();page=self.app.settings.diagnostic_page
        widgets=[child for frame in page.winfo_children() for child in frame.winfo_children()]
        self.assertEqual(sum(w.winfo_class()=='TScrollbar' for w in widgets),4)
        listing=next(w for w in widgets if w.winfo_class()=='Listbox');text=next(w for w in widgets if w.winfo_class()=='Text')
        self.assertEqual(str(text['state']),'disabled');self.assertEqual(text['wrap'],'none')
        listing.yview_moveto(1);text.yview_moveto(1);text.xview_moveto(1);self.root.update()
        self.assertGreater(listing.yview()[0],0);self.assertGreater(text.yview()[0],0);self.assertGreater(text.xview()[0],0)
