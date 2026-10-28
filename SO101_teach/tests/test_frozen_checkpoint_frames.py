"""A checkpoint owns captured evidence even when inference finishes during motion."""
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
import numpy as np
from so101_teach.episode_inspection_runtime import InspectionBatch

CHECK={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':10.}

class FrozenFrameTests(unittest.TestCase):
    def setUp(self):
        self.now=10.;timer=patch('time.monotonic',lambda:self.now);timer.start();self.addCleanup(timer.stop)
        self.worker=SimpleNamespace(result=None,submit=Mock(return_value=True),close=Mock())
        self.passed=Mock();self.batch=InspectionBatch('A',lambda s:self.worker,self.passed)
        self.addCleanup(self.batch.close)
        self.step={'id':'lower','name':'하단 검사','inspection':CHECK}
        self.batch.start(self.step)
    def capture(self,at,pixel=1):
        self.now=at;frame=np.full((2,2,3),pixel,dtype=np.uint8)
        self.batch.poll((frame,at));return frame
    def complete(self,at,now,**changes):
        self.now=now;self.worker.result={'at':at,'completed':now,'row':{
            'quality':'normal','state':'housing_seated','product':'A','product_certain':True,'reason':'normal',**changes}}
        self.batch.poll()
    def test_only_frozen_checkpoint_frames_reach_worker(self):
        first=self.capture(10.1);first[:]=99;self.capture(10.2,2)
        self.complete(10.1,11.5)
        self.capture(16.,200)  # next motion is covering the inspection target
        sent=self.worker.submit.call_args_list
        self.assertEqual([c.args[1] for c in sent],[10.1,10.2])
        self.assertTrue((sent[0].args[0]==1).all())
    def test_valid_capture_survives_queue_and_inference_age_over_three_seconds(self):
        self.capture(10.1);self.capture(10.2)
        self.complete(10.1,12.1);self.complete(10.2,14.1)
        self.passed.assert_called_once()
    def test_prearrival_duplicate_and_old_received_images_do_not_fill_capture(self):
        frame=np.zeros((2,2,3));self.now=10.1;self.batch.poll((frame,9.9))
        self.capture(10.2);self.batch.poll((frame,10.2))
        self.now=11.5;self.batch.poll((frame,10.3))
        self.assertTrue(self.batch.capturing);self.worker.submit.assert_not_called()
        self.capture(11.6);self.assertFalse(self.batch.capturing)
    def test_no_camera_cannot_extend_capture_into_another_stage(self):
        self.capture(10.1);self.now=12.
        with self.assertRaisesRegex(ValueError,'영상 확보 시간 초과'):self.batch.poll()
        self.worker.submit.assert_not_called()
    def test_unknown_frames_fail_without_using_later_clear_frame(self):
        self.capture(10.1);self.capture(10.2)
        self.complete(10.1,11.,quality='waiting',state='unknown')
        with self.assertRaisesRegex(ValueError,'검사 시점 영상 판정 불가'):
            self.complete(10.2,12.,quality='waiting',state='unknown')
        self.passed.assert_not_called()
    def test_wrong_stage_still_fails_even_after_long_processing(self):
        self.capture(10.1);self.capture(10.2)
        self.complete(10.1,12.,state='insert_added')
        with self.assertRaisesRegex(ValueError,'불합격'):self.complete(10.2,15.,state='insert_added')
    def test_processing_stall_is_bounded_separately_from_capture(self):
        self.capture(10.1);self.capture(10.2);self.now=20.21
        with self.assertRaisesRegex(ValueError,'영상 처리 시간 초과'):self.batch.poll()
    def test_result_from_unsubmitted_capture_cannot_pass(self):
        self.capture(10.1);self.capture(10.2)
        self.complete(11.,12.);self.complete(11.1,13.)
        self.passed.assert_not_called();self.assertEqual(self.worker.submit.call_count,1)
    def test_late_poll_can_consume_completed_in_time_final_result(self):
        self.capture(10.1);self.capture(10.2);self.complete(10.1,12.)
        self.worker.result={'at':10.2,'completed':19.,'row':{'quality':'normal','state':'housing_seated','product':'A','product_certain':True}}
        self.now=21.;self.batch.poll();self.passed.assert_called_once()
    def test_minimum_observations_are_all_captured_before_dispatch(self):
        self.batch.close();self.step['inspection']={**CHECK,'minimum_observations':3};self.batch.start(self.step)
        self.capture(10.1);self.capture(10.2);self.worker.submit.assert_not_called()
        self.capture(10.3);self.assertFalse(self.batch.capturing)
        for at,now in ((10.1,12.),(10.2,14.),(10.3,16.)):self.complete(at,now)
        self.passed.assert_called_once()
    def test_pause_never_recaptures_previous_checkpoint(self):
        self.capture(10.1);self.capture(10.2);self.batch.restart()
        self.complete(10.1,12.);self.complete(10.2,14.);self.passed.assert_called_once()
    def test_pause_while_capturing_fails_without_new_position_evidence(self):
        self.capture(10.1)
        with self.assertRaisesRegex(ValueError,'영상 확보 중 중단'):self.batch.restart()

class PiCaptureBoundaryTests(unittest.TestCase):
    def exercise(self,provide_frames=True,changed_epoch=False):
        from tests.test_episode_cli import cli,FakeLink,FakeLinear
        self.link=FakeLink();self.link.mode='HOLD';self.now=100.;self.events=[]
        self.runner=cli.Runner(self.link,self.events.append,cli.Control(),20,linear=FakeLinear())
        if changed_epoch:self.runner.planned_epoch=0
        worker=SimpleNamespace(result=None,submit=Mock(return_value=True),close=Mock())
        batch=InspectionBatch('A',lambda s:worker,lambda s:None);self.addCleanup(batch.close)
        self.runner.inspection_batch=batch
        def poll():
            capturing=batch.capturing
            view=(np.zeros((2,2,3)),self.now) if batch.capturing and provide_frames else None
            if batch.capturing:self.assertEqual(self.link.mode,'HOLD')
            if batch.pending and batch.pending[0]['inflight'] is not None:
                worker.result={'at':batch.pending[0]['inflight'],'completed':self.now,'row':{'quality':'normal','state':'housing_seated','product':'A','product_certain':True}}
            batch.poll(view)
            if capturing and not batch.capturing and changed_epoch:self.runner.preparation_epoch+=1
        original=self.link.rpc
        def rpc(method,args=None):
            if method=='command' and args['action']=='play' and any(m=='command' and a['action']=='play' for m,a in self.link.calls):
                self.assertFalse(batch.pending)
                self.assertIsNotNone(worker.result)  # no next motion before PASS
            return original(method,args)
        steps=[{'id':'a','name':'검사 위치','inspection':CHECK},{'id':'b','name':'다음 위치'}]
        with patch('time.monotonic',lambda:self.now),patch('time.sleep',lambda t:setattr(self,'now',self.now+t)),patch.object(self.runner,'retry_visual',side_effect=lambda operation,**kw:operation()),patch.object(self.runner,'poll_inspections',side_effect=poll),patch.object(self.link,'rpc',side_effect=rpc):
            self.runner.move('play',[{'q':1},{'q':2}],steps,{})
    def test_next_segment_waits_for_capture_and_successful_inference(self):
        self.exercise()
        targets=[a['targets'] for m,a in self.link.calls if m=='command' and a['action']=='play']
        self.assertEqual(targets,[[{'q':1}],[{'q':2}]])
    def test_missing_frames_prevent_next_segment(self):
        with self.assertRaisesRegex(ValueError,'영상 확보 시간 초과'):self.exercise(False)
        self.assertEqual(sum(m=='command' and a['action']=='play' for m,a in self.link.calls),1)
    def test_later_segment_does_not_replay_completed_work_after_resume_epoch_changes(self):
        self.exercise(changed_epoch=True)
        self.assertEqual([a['targets'] for m,a in self.link.calls if m=='command' and a['action']=='play'],[[{'q':1}],[{'q':2}]])
    def test_preparation_changed_before_first_motion_still_requires_replan(self):
        from tests.test_episode_cli import cli,FakeLink,FakeLinear
        link=FakeLink();run=cli.Runner(link,lambda e:None,cli.Control(),20,linear=FakeLinear())
        run.planned_epoch=0;run.preparation_epoch=1
        with self.assertRaises(cli.PreparationRestart):run.move('play',[{}])
        self.assertFalse(any(m=='command' and a.get('action')=='play' for m,a in link.calls))

class ThreadedSnapshotTests(unittest.TestCase):
    def test_real_worker_queue_keeps_capture_after_slow_inference_and_later_images(self):
        import threading,time
        from so101_teach.episode_inspection_runtime import FrameInspection
        seen=[];passed=Mock();workers=[]
        def inspect(frame,*args,**kwargs):
            seen.append(int(frame[0,0,0]))
            threading.Event().wait(2.)  # deterministic slow classifier, no hardware
            return [{'quality':'normal','state':'housing_seated','product':'A','product_certain':True}]
        inspector=SimpleNamespace(inspect=inspect,close=lambda:None)
        def factory(step):
            worker=FrameInspection('.',{},SimpleNamespace(items={}),{'linear_stage':{'startup_state':{'known':True}}},CHECK,'A',fixed_jigs={})
            workers.append(worker);return worker
        located={'linear':([{'id':'linear_100_0','inspection_allowed':True}],'fixed')}
        with patch('so101_teach.ai_inspection.inspector_for',return_value=inspector),patch('so101_teach.episode_inspection_runtime.detect_inspection_frame',return_value=({},None)),patch('so101_teach.inspection_geometry.inspection_context',return_value=(located,[])):
            batch=InspectionBatch('A',factory,passed)
            try:
                batch.start({'id':'s','name':'snapshot','inspection':CHECK})
                for pixel in (1,2):
                    batch.poll((np.full((2,2,3),pixel,dtype=np.uint8),time.monotonic()))
                    threading.Event().wait(.02)
                deadline=time.monotonic()+8
                while batch.pending and time.monotonic()<deadline:
                    batch.poll((np.full((2,2,3),99,dtype=np.uint8),time.monotonic()))
                    threading.Event().wait(.02)
                self.assertFalse(batch.pending);self.assertEqual(seen,[1,2])
                capture=passed.call_args.args[0]['inspection_capture']
                self.assertGreater(capture['processed_at']-capture['captured_at'][1],3.)
            finally:
                batch.close()
                for worker in workers:worker.close();worker.join(3)
