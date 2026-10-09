import unittest
from so101_teach.adoption_display import adoption_text

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
