import unittest
from copy import deepcopy
import numpy as np
from so101_teach.display_pose import DisplayPoses

def item(x=0,yaw=0):
    return {'center_px':[x,100.],'quad':[[x-10,90],[x+10,90],[x+10,110],[x-10,110]],'image_angle_deg':yaw,'symmetry_deg':90,
            'metric':{'center_xy_mm':[x,20.],'yaw_deg':yaw,'symmetry_deg':90},'axes_px':[[0,0]]*5}

class DisplayPoseTests(unittest.TestCase):
    def test_only_display_moves_while_measurement_stays_exact(self):
        d=DisplayPoses(.4);old=item();target=item(20,10);original=deepcopy(target)
        self.assertEqual(d.update('a',old,0),old)
        self.assertEqual(d.update('a',target,1)['metric']['center_xy_mm'][0],0)
        mid=d.update('a',target,1.2);self.assertAlmostEqual(mid['metric']['center_xy_mm'][0],10)
        end=d.update('a',target,1.4);self.assertAlmostEqual(end['metric']['center_xy_mm'][0],20)
        self.assertEqual(target,original)
    def test_angle_wrap_and_corner_reordering_do_not_spin_or_collapse(self):
        d=DisplayPoses(.4);old=item(0,89);target=item(0,1);target['quad']=np.roll(target['quad'],2,axis=0).tolist()
        d.update('a',old,0);d.update('a',target,1)
        mid=d.update('a',target,1.2);self.assertAlmostEqual(mid['metric']['yaw_deg'],90)
        np.testing.assert_allclose(mid['quad'],old['quad'])
        self.assertAlmostEqual(d.update('a',target,2)['metric']['yaw_deg'],91)
    def test_retarget_starts_at_current_display_and_jigs_are_independent(self):
        d=DisplayPoses(.4);d.update('a',item(),0);d.update('a',item(20),1)
        before=d.update('a',item(20),1.2);after=d.update('a',item(40),1.2)
        self.assertEqual(before['center_px'],after['center_px'])
        self.assertEqual(d.update('b',item(70),1.2)['center_px'][0],70)
    def test_execution_snaps_to_confirmed_value_and_missing_detection_is_not_invented(self):
        d=DisplayPoses(.4);d.update('a',item(),0);d.update('a',item(20),1)
        fixed=d.update('a',item(20),1.1,frozen=True);self.assertEqual(fixed['center_px'][0],20)
        self.assertIsNone(d.update('a',None,1.2))
        self.assertEqual(d.update('a',item(50),10)['center_px'][0],50)
        d.clear();self.assertFalse(d.states)
    def test_curved_outline_moves_with_center_even_when_corners_reorder(self):
        d=DisplayPoses(.4);old=item();target=item(20)
        def border(q):
            q=np.asarray(q);return np.concatenate([a+(b-a)*np.linspace(0,1,24,endpoint=False)[:,None] for a,b in zip(q,np.roll(q,-1,axis=0))]).tolist()
        old['outline_px']=border(old['quad']);target['quad']=np.roll(target['quad'],2,axis=0).tolist();target['outline_px']=border(target['quad'])
        d.update('a',old,0);d.update('a',target,1);mid=d.update('a',target,1.2)
        np.testing.assert_allclose(np.asarray(mid['outline_px'])-old['outline_px'],np.tile([10,0],(96,1)),atol=1e-6)
        self.assertAlmostEqual(mid['center_px'][0],10)
