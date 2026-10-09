import fcntl
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from so101_teach.assembly_evidence import AssemblyEvidence,invalidate_manual_motion
from so101_teach.episode_inspection import InspectionDecision
from so101_teach.inspection_seating import apply_quality

CHECK={'station':'linear','target':'2','expected':'cap_added','timeout_seconds':5.}
TOP={'state':'cap_visible','quality':'normal','product':'B','product_certain':True,'seating':'inside','reason':'높이 미확인'}


def stage(kind):return {'id':kind,'inspection':{**CHECK,'expected':kind}}

def observe(evidence,row=TOP,check=CHECK):
    decision=InspectionDecision(check,'B',0,evidence=evidence)
    first=decision.observe({'at':.1,'row':deepcopy(row)},.2)
    second=decision.observe({'at':.3,'row':deepcopy(row)},.4)
    return first,second,decision


class BLinearEvidenceTests(unittest.TestCase):
    def test_current_b_shape_passes_without_prior_checks(self):
        first,passed,decision=observe(AssemblyEvidence())
        self.assertFalse(first);self.assertTrue(passed)
        self.assertEqual(decision.basis['basis'],'current_visual')
        self.assertNotIn('이력',decision.reason)
        self.assertTrue(observe(None)[1])

    def test_old_missing_or_corrupt_history_cannot_change_result(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'assembly-evidence.json'
            self.assertTrue(observe(AssemblyEvidence(path))[1])
            for data in ('not-json','{"schema":1,"receipt":{"episode_id":"old"}}'):
                path.write_text(data)
                self.assertTrue(observe(AssemblyEvidence(path,scope={'criteria':'new'}))[1])

    def test_diagnostics_can_be_recorded_without_becoming_a_precondition(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'assembly-evidence.json'
            build=AssemblyEvidence(path,episode_id='build')
            for kind in ('housing_seated','insert_added','cap_added'):build.record(stage(kind),'B')
            build.commit();self.assertTrue(path.exists())
            transport=AssemblyEvidence(path);self.assertIsNone(transport.receipt)
            self.assertTrue(observe(transport)[1])

    def test_bad_seating_and_wrong_product_still_fail(self):
        for row in ({**TOP,'quality':'abnormal','seating':'boundary'},
                    {**TOP,'quality':'abnormal','product':'A'}):
            with self.assertRaisesRegex(ValueError,'불합격'):observe(AssemblyEvidence(),row)

    def test_manual_motion_invalidates_receipt_but_episode_lock_preserves_it(self):
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder);path=data/'assembly-evidence.json';path.write_text('{"schema":1,"receipt":{"test":true}}')
            with (data/'episode-cli.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);invalidate_manual_motion(data)
                self.assertIn('test',path.read_text())
            invalidate_manual_motion(data);self.assertIn('"receipt": null',path.read_text())

    def test_finished_pallet_and_a_keep_height_discrimination(self):
        for product,part in [('B','finished'),('A','assembly')]:
            row={'state':'cap_only','product':product,'product_certain':True,'reason':'형상 비교'}
            apply_quality(row,{'part':part},'inside')
            self.assertEqual(row['quality'],'abnormal');self.assertIn('미완성',row['label'])
        evidence=AssemblyEvidence()
        for kind in ('housing_seated','insert_added'):evidence.record(stage(kind),'B')
        with self.assertRaisesRegex(ValueError,'불합격'):
            observe(evidence,{'state':'cap_only','quality':'abnormal','product':'B','product_certain':True,'reason':'단품'}, {**CHECK,'station':'finished','target':'pallet'})
