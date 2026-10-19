"""Model search is a recovery operation; every tracked pose still needs a new rim."""
from copy import deepcopy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
import numpy as np
from so101_teach.vision_service import MultiDetector
from so101_teach.model_regions import tracking_regions
from tests.test_model_regions import SETTINGS


def found(x=20):
    item={'quad':[[x,20],[x+30,20],[x+30,50],[x,50]]}
    return {'selected':item,'candidates':[item],'status':'shape_match'}
MISSING={'selected':None,'candidates':[],'status':'not_found'}

class ModelReacquisitionTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.zeros((100,200,3),np.uint8)
        self.catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda key:{'shape':'ring'})
        self.detector=MultiDetector(self.catalog,{'jig_detection':deepcopy(SETTINGS)})
        self.provider=Mock();self.provider.predict.return_value=[{'class_id':1,'confidence':.9,'box':[20,20,50,50]}]
        self.detector.region_model=self.provider;self.detector.refresh()
        self.detector.latches['pallet'].update=lambda raw,*a,**kw:raw
    def tearDown(self):self.detector.close()
    @contextmanager
    def cv(self,**kwargs):
        mocked=Mock(**kwargs)
        def seeded(*args,**kw):return mocked(*args,**kw) if args[4] else deepcopy(MISSING)
        with patch('so101_teach.vision_service.detect',mocked),patch('so101_teach.vision_service.detect_mask_seeded',seeded):yield mocked
    def test_normal_tracking_moves_window_without_more_model_calls(self):
        with self.cv(side_effect=[found(20),found(30),found(40)]):
            results=[self.detector.process(self.frame) for _ in range(3)]
        self.assertEqual(self.provider.predict.call_count,1)
        self.assertEqual([r['model_inference_called'] for r in results],[True,False,False])
        self.assertEqual(results[-1]['tracking_state'],'tracking')
        self.assertEqual(self.detector.region_tracks['pallet'][0]['box'][0],24)
    def test_lost_track_reacquires_on_same_frame_and_resets_adoption(self):
        latch=self.detector.latches['pallet'];latch.clear=Mock(wraps=latch.clear)
        with self.cv(side_effect=[found(),MISSING,MISSING,found(100)]):
            self.detector.process(self.frame);result=self.detector.process(self.frame)
        self.assertEqual(self.provider.predict.call_count,2)
        self.assertEqual(result['tracking_state'],'reacquired');latch.clear.assert_called_once()
        self.assertEqual(result['selected']['quad'][0][0],100)
    def test_failure_clears_pose_and_rate_limits_absent_jig(self):
        with self.cv(side_effect=[found(),MISSING,MISSING]),patch('so101_teach.vision_service.time.monotonic',return_value=100.):
            self.detector.process(self.frame);self.provider.predict.return_value=[]
            lost=self.detector.process(self.frame);waiting=self.detector.process(self.frame)
        self.assertIsNone(lost['selected']);self.assertIsNone(waiting['selected'])
        self.assertFalse(waiting['model_inference_called']);self.assertFalse(self.detector.region_tracks)
        with patch('so101_teach.vision_service.time.monotonic',return_value=101.01):self.detector.process(self.frame)
        self.assertEqual(self.provider.predict.call_count,3)
    def test_model_box_without_opencv_rim_never_becomes_a_track(self):
        with self.cv(return_value=MISSING):result=self.detector.process(self.frame)
        self.assertIsNone(result['selected']);self.assertFalse(self.detector.region_tracks)
    def test_clear_calibration_roi_and_model_changes_invalidate_track(self):
        with self.cv(side_effect=lambda *a,**kw:found()):
            self.detector.process(self.frame)
            self.detector.clear();self.detector.process(self.frame)
            self.detector.profile['table_z_mm']=25;self.detector.process(self.frame)
            self.catalog.items['pallet']['roi']=[0,0,1,1];self.detector.process(self.frame)
            self.detector.profile['jig_detection']['confidence']=.6;self.detector.process(self.frame)
        self.assertEqual(self.provider.predict.call_count,5)
    def test_inflight_clear_discards_track_as_well_as_pose(self):
        def interrupted(*a,**kw):self.detector.clear();return found()
        with self.cv(side_effect=interrupted):result=self.detector.process(self.frame)
        self.assertEqual(result['status'],'settings_changed');self.assertFalse(self.detector.region_tracks)
    def test_multiple_jigs_share_one_model_call_and_keep_successful_track(self):
        self.catalog.items['second']={};self.catalog.revision+=1;self.detector.refresh()
        self.detector.latches['second'].update=lambda raw,*a,**kw:raw
        with self.cv(side_effect=[found(),MISSING,found(22)]),patch('so101_teach.vision_service.time.monotonic',return_value=100.):
            first=self.detector.process(self.frame);second=self.detector.process(self.frame)
        self.assertEqual(self.provider.predict.call_count,1)
        self.assertIsNotNone(second['live_by_jig']['pallet']['selected'])
        self.assertIsNone(second['live_by_jig']['second']['selected'])
    def test_tracking_uses_distorted_outline_bounds_when_present(self):
        raw=found();raw['selected']['outline_px']=[[10,10],[70,10],[70,70],[10,70]]
        self.assertEqual(tracking_regions(self.frame,raw)[0]['box'],[0,0,86,86])
    def test_local_context_retry_recovers_without_model(self):
        with self.cv(side_effect=[found(),MISSING,found(22)]):
            self.detector.process(self.frame);result=self.detector.process(self.frame)
        self.assertEqual(self.provider.predict.call_count,1)
        self.assertTrue(result['tracking_local_retry']);self.assertEqual(result['tracking_state'],'tracking')
    def test_model_error_after_loss_does_not_reuse_old_region(self):
        with self.cv(side_effect=[found(),MISSING,MISSING]):
            self.detector.process(self.frame);self.provider.predict.side_effect=RuntimeError('worker failed')
            result=self.detector.process(self.frame)
        self.assertEqual(result['status'],'model_error');self.assertIsNone(result['selected']);self.assertFalse(self.detector.region_tracks)
