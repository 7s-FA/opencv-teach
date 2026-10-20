import unittest
from so101_teach.adoption_display import adoption_text,adoption_summary,detection_reason

class AdoptionDisplayTests(unittest.TestCase):
    def test_confirmation_and_unaccepted_are_not_adopted(self):
        self.assertEqual(adoption_text({},10,5),'미채택')
        self.assertEqual(adoption_text({'selected':None,'stable_candidate_seconds':.2,'stable_candidate_required_seconds':.6},10,5),'확인 중 0.2/0.6초')
    def test_accepted_hold_uses_measurement_age(self):
        r={'selected':{'metric':{}},'pose_held':True,'pose_measured_at':8,'hold_remaining_s':9}
        self.assertEqual(adoption_text(r,10,5),'채택됨 · 유지 3.0초');self.assertEqual(adoption_text(r,14,5),'만료 · 미채택')
    def test_execution_and_teaching_holds_do_not_expire_like_timed_hold(self):
        r={'selected':{'metric':{}},'pose_held':True,'pose_measured_at':0,'pose_frozen':True}
        self.assertEqual(adoption_text(r,100,5),'채택됨 · 실행 고정');r['teaching_held']=True
        self.assertEqual(adoption_text(r,100,5),'채택됨 · 티칭 고정')
    def test_roi_rejection_survives_acquisition_progress_and_failure(self):
        from so101_teach.vision import PoseLatch
        raw={'selected':None,'candidates':[],'status':'mask_rim_not_found','cad_pallet_reason':'roi',
             'model_detected':True,'error':'팔레트가 지그 검출 ROI 범위를 벗어남'}
        latch=PoseLatch(10,3,1)
        for now in (0.,1.,3.):
            result=latch.update(raw,now)
            self.assertIsNone(result['selected'])
            text=adoption_text(result,now,10)
            self.assertIn('ROI 경계 이탈',text)
            self.assertEqual(adoption_summary(text),'ROI 경계 이탈')
    def test_detected_label_failure_is_not_reported_as_missing_label(self):
        self.assertEqual(detection_reason({'model_detected':True,'rejection_reason':'mask_too_small'}),'지그 마스크 면적 부족')
        self.assertEqual(detection_reason({'model_detected':False,'status':'model_not_found'}),'AI 지그 미검출')
        self.assertEqual(detection_reason({'selected':{'center_px':[1,2]},'cad_pallet_reason':'roi'}),'')
        self.assertEqual(detection_reason({'model_detected':True,'status':'model_not_found','error':'모델 영역과 지정 범위가 겹치지 않습니다.'}),'ROI 경계 이탈')
        self.assertEqual(detection_reason({'model_detected':True,'status':'model_not_found'}),'지그 형상 미확인')
