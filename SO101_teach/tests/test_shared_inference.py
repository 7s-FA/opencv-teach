from copy import deepcopy
from types import SimpleNamespace
import threading,time,tempfile,unittest
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.shared_inference import FrameInference,shared_frame
from so101_teach.ai_inspection import ModelInspector
from so101_teach.episode_inspection import InspectionDecision
from tests.test_model_regions import SETTINGS


class FrameInferenceTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.zeros((40,60,3),np.uint8)
        self.predict=Mock(return_value=[{'class_id':1,'mask':np.ones((40,60),bool)}])
        self.shared=FrameInference(self.frame,SETTINGS,self.predict)
    def test_raw_copy_reuses_one_inference_without_sharing_mutable_masks(self):
        first=self.shared.get(self.frame,SETTINGS)
        first[0]['mask'][:]=False;first[0]['class_id']=12
        second=self.shared.get(self.frame.copy(),SETTINGS)
        self.assertTrue(second[0]['mask'].all());self.assertEqual(second[0]['class_id'],1)
        self.predict.assert_called_once();self.assertEqual(self.shared.hits,1)
    def test_new_capture_gets_new_inference_even_when_pixels_are_identical(self):
        self.shared.get(self.frame,SETTINGS)
        FrameInference(self.frame.copy(),SETTINGS,self.predict).get(self.frame,SETTINGS)
        self.assertEqual(self.predict.call_count,2)
    def test_changed_pixels_shape_dtype_or_settings_never_use_old_evidence(self):
        self.shared.get(self.frame,SETTINGS)
        changed=self.frame.copy();changed[0,0,0]=1
        for frame in (changed,self.frame[:20],self.frame.astype(np.float32)):
            self.assertIsNone(self.shared.get(frame,SETTINGS))
        for key,value in (('model','s'),('model_identity','b'*64),('confidence',.6),('method','opencv')):
            self.assertIsNone(self.shared.get(self.frame,{**SETTINGS,key:value}))
        self.predict.assert_called_once()
    def test_empty_detections_are_shared_but_failed_predictions_are_not_cached(self):
        self.predict.side_effect=[RuntimeError('inference failed'),[],[]]
        with self.assertRaisesRegex(RuntimeError,'inference failed'):self.shared.get(self.frame,SETTINGS)
        self.assertEqual(self.shared.get(self.frame,SETTINGS),[])
        self.assertEqual(self.shared.get(self.frame,SETTINGS),[])
        self.assertEqual(self.predict.call_count,2)
    def test_input_changed_during_prediction_is_discarded(self):
        def predict(frame,settings):
            frame[0,0,0]=1
            return [{'class_id':1}]
        self.predict.side_effect=predict
        self.assertIsNone(self.shared.get(self.frame,SETTINGS))
        self.assertIsNone(self.shared.rows)
    def test_simultaneous_consumers_do_not_run_duplicate_inference(self):
        entered=threading.Event();release=threading.Event()
        def predict(*args):entered.set();release.wait(2);return []
        self.predict.side_effect=predict
        results=[]
        first=threading.Thread(target=lambda:results.append(self.shared.get(self.frame,SETTINGS)))
        second=threading.Thread(target=lambda:results.append(self.shared.get(self.frame.copy(),SETTINGS)))
        first.start();self.assertTrue(entered.wait(1));second.start();release.set()
        first.join(2);second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive());self.assertEqual(results,[[],[]])
        self.predict.assert_called_once()
    def test_different_jig_and_part_models_keep_separate_predictions(self):
        profile={'jig_detection':SETTINGS,'part_inspection':{**SETTINGS,'model':'s'}}
        self.assertIsNone(shared_frame(self.frame,profile,Mock()))
        self.assertIsNone(shared_frame(self.frame,{'jig_detection':SETTINGS,'part_inspection':{'method':'opencv'}},Mock()))


class SharedInspectionTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.zeros((40,60,3),np.uint8)
        self.profile={'jig_detection':deepcopy(SETTINGS),'part_inspection':deepcopy(SETTINGS)}
        self.provider=Mock();self.provider.predict.return_value=[]
        self.geometry=Mock()
        self.geometry.inspect.return_value=[{'id':'linear_0','quality':'normal','state':'housing_seated','product':'A','product_certain':True}]
        self.inspector=ModelInspector(self.geometry,self.provider)
    def test_model_inspector_reinfers_mismatched_frame_and_still_checks_geometry(self):
        inference=shared_frame(self.frame,self.profile,self.inspector)
        inference.get(self.frame,SETTINGS)
        changed=self.frame.copy();changed[0,0,0]=1
        with patch('so101_teach.ai_inspection.combine',side_effect=lambda rows,*args,**kwargs:rows):
            self.inspector.inspect(changed,[{'id':'linear_0'}],self.profile,inference=inference)
        self.assertEqual(self.provider.predict.call_count,2)
        self.geometry.inspect.assert_called_once()
    def test_prepared_model_can_supply_both_consumers_without_deadlock(self):
        from so101_teach.episode_inspection_runtime import PreparedInspector
        with patch('so101_teach.ai_inspection.inspector_for',return_value=self.inspector):
            session=PreparedInspector(self.profile);self.assertTrue(session.ready.wait(2))
        try:
            borrower=session.borrow()
            evidence=shared_frame(self.frame,self.profile,borrower)
            with patch('so101_teach.ai_inspection.combine',side_effect=lambda rows,*args,**kwargs:rows):
                borrower.inspect(self.frame,[{'id':'linear_0'}],self.profile,inference=evidence)
                evidence.get(self.frame,SETTINGS)
            self.provider.predict.assert_called_once()
        finally:session.close()
    def test_runtime_shares_per_frame_and_requires_fresh_matching_observations(self):
        from so101_teach.episode_inspection_runtime import FrameInspections
        from tests.test_episode_inspection import CHECK
        profile=self.profile;inspector=self.inspector
        class Detector:
            def process(self,frame,*,inference):
                self.evidence=inference
                inference.get(frame,SETTINGS)
                return {'live_by_jig':{}}
            def close(self):pass
        detector=Detector();anchor={'id':'linear_0','inspection_allowed':True}
        step={'id':'one','name':'one','inspection':CHECK,'inspection_product':'A'}
        started=time.monotonic();decision=InspectionDecision(CHECK,'A',started)
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.ai_inspection.inspector_for',return_value=inspector),patch('so101_teach.vision_service.MultiDetector',return_value=detector),patch('so101_teach.inspection_geometry.anchors_for',return_value=([anchor],'test')),patch('so101_teach.ai_inspection.combine',side_effect=lambda rows,*args,**kwargs:rows):
            worker=FrameInspections(folder,profile,SimpleNamespace(items={}),{'linear_stage':{'startup_state':{'known':True}}},[step])
            try:
                # A contradictory second observation resets the signature; the
                # later good frame must not pair with the first good frame.
                for index,quality in enumerate(('normal','abnormal','normal','normal')):
                    self.geometry.inspect.return_value[0]['quality']=quality
                    at=time.monotonic();worker.result=None;worker.submit(self.frame,at)
                    until=time.monotonic()+2
                    while worker.result is None and time.monotonic()<until:time.sleep(.005)
                    self.assertIsNotNone(worker.result);self.assertNotIn('error',worker.result)
                    self.assertEqual(self.provider.predict.call_count,index+1)
                    observation=worker.result['results']['one']
                    passed=decision.observe(observation,time.monotonic())
                    self.assertEqual(passed,index==3)
                    if index==0:
                        self.assertFalse(decision.observe(observation,time.monotonic()))
                        self.assertEqual(decision.count,1)
                    self.assertEqual(detector.evidence.calls,1)
                    self.assertEqual(detector.evidence.hits,1)
            finally:worker.close();worker.join(2)
