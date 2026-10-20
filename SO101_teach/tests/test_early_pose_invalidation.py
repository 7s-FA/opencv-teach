"""An unfrozen early pose must stop being usable after a conflicting observation."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.vision import PoseLatch
from so101_teach.vision_service import MultiDetector
from tests.test_cad_three_frame_gate import cad_observation
from tests.test_two_frame_gate import observation,SETTINGS


def carrier(identity):
    raw=observation();raw['carrier_observation_id']=identity;return raw


class EarlyPoseInvalidationTests(unittest.TestCase):
    def adopted(self,make=cad_observation):
        latch=PoseLatch(10,4,5);latch.clear(0.)
        for i,t in enumerate((.5,1.,1.5)):out=latch.update(make(str(i)),t)
        self.assertIsNotNone(out['selected']);return latch,out

    def test_moved_early_pose_disappears_immediately_and_requires_three_new_votes(self):
        latch,_=self.adopted()
        changed=latch.update(cad_observation('moved1',140),2.)
        self.assertIsNone(changed['selected']);self.assertFalse(changed['pose_held'])
        self.assertTrue(changed['pose_invalidated']);self.assertIsNone(latch.poll(2.1)['selected'])
        self.assertIsNone(latch.update(cad_observation('moved2',140),2.5)['selected'])
        new=latch.update(cad_observation('moved3',140),3.)
        self.assertEqual(new['selected']['center_px'][0],140)
        self.assertEqual(new['pose_measured_at'],3.)

    def test_rotation_and_scale_conflicts_also_invalidate(self):
        for change in (lambda r:r['selected'].update(image_angle_deg=20),lambda r:r['selected'].update(area_px=5000)):
            with self.subTest(change=change):
                latch,_=self.adopted();raw=cad_observation('changed');change(raw)
                self.assertIsNone(latch.update(raw,2.)['selected'])

    def test_carrier_metric_shift_invalidates_even_when_pixels_agree(self):
        for field in ('center','angle'):
            with self.subTest(field=field):
                latch,_=self.adopted(carrier);raw=carrier('changed')
                if field=='center':raw['selected']['metric']['center_xy_mm'][0]+=.6
                else:raw['selected']['metric']['yaw_deg']+=.6
                self.assertIsNone(latch.update(raw,2.)['selected'])

    def test_small_jitter_and_square_angle_wrap_keep_the_original_measurement(self):
        latch,first=self.adopted()
        current=latch.update(cad_observation('jitter',102,11),2.)
        self.assertEqual(current['selected'],first['selected']);self.assertEqual(current['pose_measured_at'],1.5)
        latch=PoseLatch(10,4,5);latch.clear(0)
        for i,t in enumerate((.5,1.,1.5)):latch.update(cad_observation(str(i),100,89),t)
        self.assertIsNotNone(latch.update(cad_observation('wrap',100,1),2.)['selected'])

    def test_invalidation_discards_a_queued_old_pose_and_old_votes(self):
        latch,_=self.adopted()
        for i,t in enumerate((1.7,1.8,1.9)):latch.update(cad_observation('old'+str(i)),t)
        self.assertIsNotNone(latch.next_saved)
        self.assertIsNone(latch.update(cad_observation('moved',140),2.)['selected'])
        self.assertIsNone(latch.next_saved);self.assertIsNone(latch.poll(20.)['selected'])

    def test_execution_freeze_keeps_the_explicit_execution_reference(self):
        latch,first=self.adopted();latch.freeze(True)
        out=latch.update(cad_observation('moved',140),2.)
        self.assertEqual(out['selected'],first['selected']);self.assertTrue(out['pose_frozen'])

    def test_duplicate_timestamp_and_held_input_do_not_count_as_new_motion(self):
        for held,at in ((False,1.5),(True,2.)):
            with self.subTest(held=held):
                latch,first=self.adopted();raw=cad_observation('not_fresh',140);raw['pose_held']=held
                out=latch.update(raw,at);self.assertEqual(out['selected'],first['selected'])

    def test_detector_and_poll_publish_no_pose_on_the_changed_frame(self):
        catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda _: {'shape':'ring'})
        detector=MultiDetector(catalog,{'jig_detection':deepcopy(SETTINGS)},default_latch=PoseLatch(10,4,5))
        detector.region_model=Mock();detector.region_model.predict.return_value=[]
        self.addCleanup(detector.close)
        for i,x in enumerate((100,100,100,140)):
            frame=np.zeros((720,1280,3),np.uint8);frame[0,0]=i
            with patch('so101_teach.vision_service.detect_mask_seeded',return_value=cad_observation(str(i),x)),patch('so101_teach.vision_service.time.monotonic',return_value=.5*(i+1)):
                out=detector.process(frame)
            if i==2:self.assertIsNotNone(out['selected'])
        self.assertEqual(out['live_by_jig']['pallet']['selected']['center_px'][0],140)
        self.assertIsNone(out['selected']);self.assertIsNone(detector.finish_observation(out,2.1)['selected'])
