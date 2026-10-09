import unittest
from copy import deepcopy
from so101_teach.vision import PoseLatch

def result(x=100,a=10,strong=False):
    c={'quad':[[x-10,90],[x+10,90],[x+10,110],[x-10,110]],'center_px':[x,100],
       'image_angle_deg':a,'area_px':4000,'metric':{'center_xy_mm':[x,100],'yaw_deg':a},'score':.45,'outer_shape':{'valid':True},'shape_match':strong}
    return {'selected':c if strong else None,'candidates':[c],'status':'shape_match' if strong else 'not_found'}

class PoseTests(unittest.TestCase):
    def test_weak_candidate_becomes_observation_only_after_three_seconds(self):
        latch=PoseLatch()
        for t in (0,.8,1.6,2.4):self.assertIsNone(latch.update(result(),t)['selected'])
        out=latch.update(result(x=101),3.1)
        self.assertEqual(out['status'],'stable_candidate');self.assertTrue(out['selected']['temporal_match'])
        self.assertFalse(out['verified_for_motion'])
    def test_missing_latest_multiple_candidates_and_stalled_stream_do_not_adopt(self):
        for change in ('multiple','missing'):
            latch=PoseLatch()
            for t in (0,.8,1.6,2.4):latch.update(result(),t)
            r=result()
            if change=='multiple':r['candidates'].append(result(x=180)['candidates'][0])
            if change=='missing':r['candidates']=[]
            out=latch.update(r,5 if change=='stale' else 3.1);self.assertIsNone(out['selected'])
    def test_square_angle_wrap_does_not_break_consensus(self):
        latch=PoseLatch()
        for t,a in ((0,89),(.8,0),(1.6,1),(2.4,0),(3.1,0)):out=latch.update(result(a=a),t)
        self.assertIsNotNone(out['selected'])
    def test_ten_second_hold_and_execution_freeze_do_not_follow_new_frames(self):
        latch=PoseLatch()
        for t in (0,1,2,3):first=latch.update(result(strong=True),t)
        self.assertEqual(first['selected']['center_px'][0],100)
        for t in (10.1,11.1,12.1):self.assertEqual(latch.update(result(200,strong=True),t)['selected']['center_px'][0],100)
        out=latch.update(result(200,strong=True),13.2);self.assertEqual(out['selected']['center_px'][0],200)
        latch.freeze(True);self.assertFalse(latch.clear());out=latch.update(result(300,strong=True),100)
        self.assertEqual(out['selected']['center_px'][0],200);self.assertTrue(out['pose_frozen']);self.assertEqual(out['pose_measured_at'],11.1)
        latch.freeze(False);self.assertIsNone(latch.update(result(300,strong=True),100.1)['selected'])
    def test_freeze_without_pose_never_adopts_mid_run_observation(self):
        latch=PoseLatch();latch.freeze(True);self.assertIsNone(latch.update(result(strong=True),10)['selected'])
        with self.assertRaises(ValueError):latch.configure(2)
        latch.freeze(False)
        for t in (11,12,13):self.assertIsNone(latch.update(result(strong=True),t)['selected'])
        self.assertIsNotNone(latch.update(result(strong=True),14)['selected'])
    def test_clear_restarts_consensus_without_reusing_old_samples(self):
        latch=PoseLatch()
        for t in (0,.8,1.6,2.4):latch.update(result(),t)
        latch.clear();self.assertIsNone(latch.update(result(),3.1)['selected'])

