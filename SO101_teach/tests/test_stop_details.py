import unittest,tempfile,time
from pathlib import Path
from types import SimpleNamespace
from tests import test_ui as ui_fixtures
from tests import test_arrival_tolerance as motion_fixtures
from so101_teach.domain import read_json

class StopEvidenceTests(unittest.TestCase):
    setUp=motion_fixtures.ArrivalToleranceTests.setUp
    snapshot=motion_fixtures.ArrivalToleranceTests.snapshot
    run_residual=motion_fixtures.ArrivalToleranceTests.run_residual
    def test_stop_file_keeps_original_target_before_hold_replaces_it(self):
        with tempfile.TemporaryDirectory() as d:
            self.s.audit_path=Path(d)/'session.json';goal={**self.ref.middle,'elbow_flex':self.ref.middle['elbow_flex']+40}
            self.run_residual(41,[goal]);file=next(Path(d).glob('stop-*.json'));saved=read_json(file)
            self.assertEqual(saved['target'],goal);self.assertEqual(saved['actual']['elbow_flex'],goal['elbow_flex']+41)
            self.assertEqual(self.s.last_goals['elbow_flex'],saved['actual']['elbow_flex']);self.assertIn('팔꿈치 +41틱',saved['detail'])

class StopNoticeTests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    def test_voltage_warning_and_recovery_do_not_overwrite_stop_reason(self):
        a=self.app;a.last_motion_stop='스텝 4/9 · 어깨 들기 +41틱';a.notice(a.last_motion_stop)
        for msg in ('리더 집게: 전압 경고','리더 집게: 전압 경고 해제'):
            a.device_notice(msg);self.assertEqual(a.message.get(),a.last_motion_stop);self.assertEqual(a.device_message.get(),msg)
        a.last_motion_stop=None;a.device_notice('전압 정상');self.assertEqual(a.message.get(),'전압 정상')
