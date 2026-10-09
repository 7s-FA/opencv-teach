import io
import unittest
from contextlib import redirect_stdout
import numpy as np
from so101_teach.inspection_seating import containment
from so101_teach.episode_inspection import InspectionDecision
from tests.test_work_monitor import monitor


class SeatingToleranceTests(unittest.TestCase):
    def test_small_edge_fringe_allowed_but_displacement_rejected(self):
        roi=np.zeros((80,80),np.uint8);roi[10:70,10:70]=255
        body=np.zeros_like(roi);body[20:60,20:60]=255;body[10,30:35]=255
        self.assertEqual(containment(body,roi),'boundary')
        detail={}
        self.assertEqual(containment(body,roi,boundary_fraction=.01,details=detail),'inside')
        self.assertEqual(detail['outside_percent'],0)
        body[9:12,20:60]=255
        self.assertEqual(containment(body,roi,boundary_fraction=.01),'boundary')
        body[:]=0;body[:5,:5]=255
        self.assertEqual(containment(body,roi,boundary_fraction=.01),'outside')

    def test_failure_retains_location_and_measured_deviation(self):
        check=dict(station='carrier',target='insert',expected='present',timeout_seconds=5)
        row=dict(quality='abnormal',state='present',product='B',product_certain=True,
                 reason='부품이 ROI 경계에 걸침',target_position_mm=[190,-130,20],
                 center_offset_mm=[4,-2],center_error_mm=4.5,center_tolerance_mm=3,
                 outside_percent=2,boundary_percent=3,boundary_tolerance_percent=1)
        d=InspectionDecision(check,'B',10);d.observe({'at':10.1,'row':row},10.2)
        with self.assertRaises(ValueError) as caught:d.observe({'at':10.3,'row':row},10.4)
        for word in ('X +190.0','Y -130.0','X +4.0, Y -2.0','허용 3 mm','ROI 밖 2.00%','기대 B'):
            self.assertIn(word,str(caught.exception))

    def test_separate_failed_summary_only_for_final_failure(self):
        output=io.StringIO()
        with redirect_stdout(output):
            for status in ('B_INSPECTION_FAILED:중단','B_FAILED:중단 · X +190.0','ACTION_RESULT:build_b:ERROR'):
                monitor.print_event(dict(at=1,arm='arm2',status=status))
        self.assertEqual(output.getvalue().count('B_FAILED: B 작업 실패'),1)
        self.assertIn('    - 중단',output.getvalue());self.assertIn('    - X +190.0',output.getvalue())

    def test_failed_empty_check_names_remaining_observed_parts(self):
        check=dict(station='linear',target='2',expected='empty',timeout_seconds=10)
        row=dict(state='insert_added',quality='normal',product='B',product_certain=True,seating='inside',reason='윗면 외곽·홈 비교')
        d=InspectionDecision(check,'B',10);d.observe({'at':10.1,'row':row},10.2)
        with self.assertRaises(ValueError) as caught:d.observe({'at':10.3,'row':row},10.4)
        self.assertIn('관측 B 하단·중단 있음',str(caught.exception));self.assertIn('기대 B 부품 없음',str(caught.exception))
