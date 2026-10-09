import time,json,unittest
from unittest.mock import patch
from tests import test_ui as fixtures

class JigExecutionDiagnosticTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def test_failure_identifies_required_jig_even_if_another_jig_was_adopted(self):
        a=self.app;self.fake_jig_camera();frame,accepted,at=a.camera.observation
        a.catalog.items['carrier']={'name':'운반용 지그'}
        failed={'selected':None,'status':'acquisition_failed','acquisition_issue':'관측 부족','acquisition_completed_attempts':3,'stable_candidate_samples':0}
        a.camera.observation=(frame,{'by_jig':{'pallet':accepted,'carrier':failed}},at)
        message=a.measurement_failure({'started':time.monotonic()-.1,'action':'play'},time.monotonic(),['carrier'])
        self.assertIn('운반용 지그',message);self.assertIn('관측 부족',message)
        report=json.loads(next((self.data/'diagnostics').glob('jig-measurement-*.json')).read_text())
        self.assertEqual(report['required_jigs'],{'carrier':'운반용 지그'});self.assertFalse(report['results']['carrier']['selected']);self.assertEqual(report['results']['carrier']['stable_candidate_samples'],0)
    def test_diagnostic_write_failure_cannot_mask_measurement_failure(self):
        a=self.app
        with patch('so101_teach.domain.atomic_json',side_effect=OSError('disk full')):
            message=a.measurement_failure({'started':time.monotonic()-20},time.monotonic(),['pallet'])
        self.assertIn('지그 측정 실패',message)
