"""Reproduce the completed B pallet's alternating strong/ambiguous observations."""
from copy import deepcopy
import json
import unittest
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.episode_inspection import InspectionDecision,GroupInspectionDecision
from so101_teach.inspection_seating import apply_quality
from so101_teach.shape_inspection import ShapeInspector

CHECK={'station':'finished','target':'pallet','expected':'cap_added','timeout_seconds':10.}
GOOD={'state':'cap_added','quality':'normal','seating':'inside','product':'B','product_certain':True,'reason':'현재 완제품 형상'}
WEAK={'state':'unknown','quality':'abnormal','seating':'inside','score':72,'reason':'후보 상태차 부족'}

def observation(at,row):return {'at':at,'row':deepcopy(row)}

class UncertainInspectionTests(unittest.TestCase):
    def test_scored_unknown_waits_but_confirmed_boundary_still_fails(self):
        row=deepcopy(WEAK);apply_quality(row,{'part':'finished'},'inside')
        self.assertEqual(row['quality'],'waiting')
        apply_quality(row,{'part':'finished'},'boundary')
        self.assertEqual(row['quality'],'abnormal')

    def test_repeated_unknown_neither_passes_nor_fails_before_deadline(self):
        decision=InspectionDecision(CHECK,'B',10)
        for at in (10.1,10.4,10.7,12.,17.):
            self.assertFalse(decision.observe(observation(at,WEAK),at+.01))
            self.assertEqual(decision.count,0)
        with self.assertRaisesRegex(ValueError,'시간 초과'):
            decision.observe(None,20.)

    def test_weak_frames_preserve_only_recent_positive_evidence(self):
        decision=InspectionDecision(CHECK,'B',10)
        for at,row in ((10.1,GOOD),(10.4,WEAK),(10.7,WEAK)):
            self.assertFalse(decision.observe(observation(at,row),at+.01))
        self.assertTrue(decision.observe(observation(11.,GOOD),11.01))

    def test_weak_frames_cannot_extend_old_evidence_or_count_duplicate_images(self):
        decision=InspectionDecision(CHECK,'B',10)
        self.assertFalse(decision.observe(observation(10.1,GOOD),10.2))
        for at in (10.4,10.7,11.,11.3):
            self.assertFalse(decision.observe(observation(at,WEAK),at+.01))
        self.assertFalse(decision.observe(observation(11.6,GOOD),11.61))
        self.assertFalse(decision.observe(observation(11.6,GOOD),11.7))
        self.assertTrue(decision.observe(observation(11.9,GOOD),11.91))

    def test_unknown_does_not_bridge_negative_evidence_or_hide_real_faults(self):
        bad={**GOOD,'state':'cap_only','quality':'abnormal'}
        decision=InspectionDecision(CHECK,'B',10)
        for at,row in ((10.1,bad),(10.4,WEAK),(10.7,bad)):
            self.assertFalse(decision.observe(observation(at,row),at+.01))
        with self.assertRaisesRegex(ValueError,'불합격'):
            decision.observe(observation(11.,bad),11.01)
        boundary={**WEAK,'seating':'boundary'}
        decision=InspectionDecision(CHECK,'B',10)
        self.assertFalse(decision.observe(observation(10.1,boundary),10.2))
        with self.assertRaisesRegex(ValueError,'불합격'):
            decision.observe(observation(10.4,boundary),10.5)

    def test_wrong_product_breaks_positive_evidence(self):
        decision=InspectionDecision(CHECK,'B',10)
        for at,row in ((10.1,GOOD),(10.4,{**GOOD,'product':'A'}),(10.7,GOOD)):
            self.assertFalse(decision.observe(observation(at,row),at+.01))
        self.assertTrue(decision.observe(observation(11.,GOOD),11.01))

    def test_late_result_cannot_pass_after_the_original_deadline(self):
        decision=InspectionDecision(CHECK,'B',10)
        self.assertFalse(decision.observe(observation(19.5,GOOD),19.6))
        with self.assertRaisesRegex(ValueError,'시간 초과'):
            decision.observe(observation(19.8,GOOD),20.)

    def test_user_confirmed_completed_product_is_normal_in_every_actual_frame(self):
        folder=ROOT/'tests/fixtures/finished-seated-20261011';cfg=json.loads((folder/'fixture.json').read_text())
        for anchor in cfg['anchors']:anchor['camera_from_local']=np.asarray(anchor['camera_from_local'])
        inspector=ShapeInspector();decision=InspectionDecision(CHECK,'B',10)
        group=GroupInspectionDecision([{'id':'finished','name':'복귀 위치','inspection':CHECK,'inspection_product':'B'}],10)
        states=[];passed=False;group_passed=False
        for i,path in enumerate(sorted(folder.glob('live-*.jpg'))):
            row=inspector.inspect(cv2.imread(str(path)),cfg['anchors'],cfg['profile'])[0]
            states.append(row['state']);self.assertEqual(row['quality'],'normal',row['reason'])
            at=10.1+cfg['frame_offsets_seconds'][i]
            if not passed:passed=decision.observe(observation(at,row),at+.01)
            if not group_passed:group_passed=bool(group.observe({'results':{'finished':observation(at,row)}},at+.01))
        self.assertEqual(states,['cap_added']*6)
        self.assertTrue(passed);self.assertTrue(group_passed)

if __name__=='__main__':unittest.main()
