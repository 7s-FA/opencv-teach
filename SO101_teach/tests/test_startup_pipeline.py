import base64
from copy import deepcopy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from so101_teach import startup_inspection as startup
from so101_teach.ai_inspection import ModelInspector


def step(key,station='carrier'):
    return {'id':key,'name':key,'inspection_product':'A','inspection':{
        'station':station,'target':'housing' if station=='carrier' else '1' if station=='linear' else 'pallet',
        'expected':'present' if station=='carrier' else 'empty',
        'timeout_seconds':20,'minimum_observations':2}}


def row(station='carrier'):
    return {'quality':'normal','state':'present' if station=='carrier' else 'empty',
        'product':'A','product_certain':True,'seating':'inside' if station=='carrier' else 'missing'}


class Worker:
    instances=[]
    def __init__(self,*args,**kwargs):
        self.steps=args[4];self.placement=args[3];self.options=kwargs;self.result=None;self.active=set();self.closed=False
        self.submissions=[];self.instances.append(self);self.accepting_frame=True
    def retain(self,ids):self.active=set(ids)
    def update_context(self,current,placement,ids):
        self.options['fixed_jigs']=deepcopy(current);self.placement=deepcopy(placement)
        self.active=set(ids);self.result=None
    def submit(self,frame,at):self.submissions.append(at)
    def close(self):self.closed=True
    def is_alive(self):return False
    def join(self):pass


class StartupPipelineTests(unittest.TestCase):
    def setUp(self):
        self.patch=patch.object(startup,'FrameInspections',Worker);self.patch.start()
        self.addCleanup(self.patch.stop);Worker.instances=[]
        self.events=[]
        self.pipeline=startup.StartupInspection([step('carrier'),step('linear','linear')],
            {},SimpleNamespace(items={}),{'linear_stage':{}},None,self.events.append,linear_target=100.)
        self.addCleanup(self.pipeline.close)
        self.jid=self.pipeline.dependencies['carrier'];self.current={self.jid:{'pose':[1,2,3]}}
        self.moving={'phase':'MOVING','target_mm':100.}
        self.stopped={'phase':'TIMED_COMPLETE','target_mm':100.}
    def poll(self,t,*,current=None,state=None,frame=None):
        self.pipeline.poll(self.current if current is None else current,frame,
            linear_state=state or self.moving,now=t)
    def result(self,t,keys=('carrier',)):
        self.pipeline.worker.result={'at':t,'results':{
            k:{'at':t,'row':row('linear' if k=='linear' else 'carrier')} for k in keys}}
    def test_partial_jig_starts_before_other_jigs_or_linear(self):
        self.poll(1)
        self.assertEqual(set(self.pipeline.decisions),{'carrier'})
        self.assertEqual(self.pipeline.worker.active,{'carrier'})
        self.assertNotIn('geometry_steps',self.pipeline.worker.options)
        self.assertFalse(self.pipeline.worker.placement['linear_stage']['startup_state']['known'])
        self.assertFalse(self.pipeline.complete)
    def test_adopted_neighbours_survive_even_without_their_own_check(self):
        pipeline=startup.StartupInspection([step('linear','linear')],{},SimpleNamespace(items={}),
            {'linear_stage':{}},None,self.events.append,linear_target=100.)
        self.addCleanup(pipeline.close)
        current={'carrier_jig':{'pose':[1,2,3]},'pallet':{'pose':[4,5,6]}}
        pipeline.poll(current,linear_state=self.stopped,now=1)
        self.assertEqual(pipeline.worker.options['fixed_jigs'],current)
        self.assertEqual(pipeline.worker.active,{'linear'})
    def test_neighbour_change_refreshes_geometry_without_resetting_target_pass(self):
        self.poll(1);self.result(1.1);self.poll(1.2);self.result(1.3);self.poll(1.4)
        self.assertIn('carrier',self.pipeline.passed);decision=self.pipeline.decisions['carrier']
        self.current['pallet']={'pose':[4,5,6]};old=self.pipeline.worker
        self.poll(2)
        self.assertIs(old,self.pipeline.worker);self.assertFalse(old.closed)
        self.assertIn('pallet',self.pipeline.worker.options['fixed_jigs'])
        self.assertIs(self.pipeline.decisions['carrier'],decision);self.assertIn('carrier',self.pipeline.passed)
        self.assertNotIn('INSPECTION_INVALIDATED:carrier',self.events)
    def test_fixed_jig_only_check_uses_current_linear_state_for_neighbours(self):
        pipeline=startup.StartupInspection([step('carrier')],{},SimpleNamespace(items={}),
            {'linear_stage':{'startup_state':{'known':True,'commanded_mm':1.5}}},None,self.events.append,linear_target=100.)
        self.addCleanup(pipeline.close)
        for at,state,known in ((1,self.moving,False),(2,self.stopped,True),(3,None,False)):
            pipeline.poll(self.current,linear_state=state,now=at)
            actual=pipeline.worker.placement['linear_stage']['startup_state']
            self.assertEqual(actual['known'],known)
            self.assertEqual(actual['commanded_mm'],100. if known else None)
    def test_linear_joins_same_worker_group_without_resetting_fixed_count(self):
        self.poll(1);self.result(1.1);self.poll(1.2)
        self.assertEqual(self.pipeline.decisions['carrier'].count,1)
        previous=self.pipeline.worker
        self.poll(2,state=self.stopped)
        self.assertIs(previous,self.pipeline.worker);self.assertFalse(previous.closed)
        self.assertEqual(self.pipeline.worker.active,{'carrier','linear'})
        self.assertEqual(self.pipeline.decisions['carrier'].count,1)
        self.result(2.1,('carrier','linear'));self.poll(2.2,state=self.stopped)
        self.assertEqual(set(self.pipeline.passed),{'carrier'})
        self.result(2.3,('linear',));self.poll(2.4,state=self.stopped)
        self.assertTrue(self.pipeline.complete)
    def test_old_frame_before_linear_arrival_does_not_count(self):
        self.poll(1);self.poll(3,state=self.stopped)
        self.result(2.9,('linear',));self.poll(3.1,state=self.stopped)
        self.assertEqual(self.pipeline.decisions['linear'].count,0)
    def test_pose_change_discards_provisional_pass_and_old_worker_result(self):
        self.poll(1);self.result(1.1);self.poll(1.2);self.result(1.3);self.poll(1.4)
        self.assertIn('carrier',self.pipeline.passed)
        old=self.pipeline.worker
        self.current[self.jid]['pose'][0]=9
        self.poll(2)
        self.assertNotIn('carrier',self.pipeline.passed)
        self.assertEqual(self.pipeline.decisions['carrier'].count,0)
        self.assertIs(old,self.pipeline.worker);self.assertIsNone(old.result)
        self.poll(2.1)
        self.assertIn('INSPECTION_INVALIDATED:carrier',self.events)
    def test_lost_jig_removes_evidence_then_requires_two_new_observations(self):
        self.poll(1);self.result(1.1);self.poll(1.2)
        self.poll(2,current={});self.assertNotIn('carrier',self.pipeline.decisions)
        self.poll(3);self.result(2.9);self.poll(3.1)
        self.assertEqual(self.pipeline.decisions['carrier'].count,0)
    def test_duplicate_stale_and_mismatching_observations_never_pass(self):
        self.poll(1);self.result(1.1);self.poll(1.2);self.poll(1.3)
        self.assertEqual(self.pipeline.decisions['carrier'].count,1)
        self.pipeline.worker.result['results']['carrier']={'at':1.4,'row':{
            **row(),'quality':'abnormal','state':'wrong_part'}}
        self.poll(1.5)
        self.assertFalse(self.pipeline.complete)
        self.assertEqual(self.pipeline.decisions['carrier'].count,1)
        self.result(1.6);self.poll(5)
        self.assertNotIn('carrier',self.pipeline.passed)
    def test_timeout_is_per_target_and_not_reset_by_other_target_joining(self):
        self.poll(1);self.poll(19,state=self.stopped)
        with self.assertRaisesRegex(ValueError,'시간 초과'):self.poll(21,state=self.stopped)
    def test_linear_movement_after_arrival_fails_closed(self):
        self.poll(1,state=self.stopped)
        with self.assertRaisesRegex(RuntimeError,'리니어 상태 변경'):self.poll(2)
    def test_bad_worker_result_propagates(self):
        self.poll(1);self.pipeline.worker.result={'error':'model failure'}
        with self.assertRaisesRegex(RuntimeError,'model failure'):self.poll(2)
    def test_new_capture_only_and_same_frame_for_all_targets(self):
        self.poll(1,state=self.stopped)
        encoded=base64.b64encode(cv2.imencode('.jpg',np.zeros((10,10,3),np.uint8))[1]).decode()
        frame={'preview_at':1.1,'preview_image':encoded}
        self.poll(1.2,state=self.stopped,frame=frame);self.poll(1.3,state=self.stopped,frame=frame)
        self.assertEqual(self.pipeline.worker.submissions,[1.1])

    def frame(self,at):
        return {'preview_at':at,'preview_image':base64.b64encode(
            cv2.imencode('.jpg',np.zeros((10,10,3),np.uint8))[1]).decode()}
    def test_no_worker_or_geometry_before_any_target_is_ready(self):
        for at in (1,2,3):self.poll(at,current={})
        self.assertIsNone(self.pipeline.worker);self.assertEqual(Worker.instances,[])
        self.poll(4)
        self.assertEqual(len(Worker.instances),1)
    def test_busy_inference_does_not_decode_or_replace_pending_frames(self):
        self.poll(1)
        with patch.object(cv2,'imdecode',wraps=cv2.imdecode) as decode:
            self.poll(1.2,frame=self.frame(1.1))
            for at in (1.3,1.4,1.5):self.poll(at+.01,frame=self.frame(at))
            self.assertEqual(decode.call_count,1)
            self.assertEqual(self.pipeline.worker.submissions,[1.1])
            self.result(1.1);self.poll(1.7,frame=self.frame(1.6))
            self.assertEqual(decode.call_count,2)
            self.assertEqual(self.pipeline.worker.submissions,[1.1,1.6])
    def test_final_observation_prevents_third_decode_and_inference(self):
        self.poll(1,state=self.stopped)
        with patch.object(cv2,'imdecode',wraps=cv2.imdecode) as decode:
            self.poll(1.2,state=self.stopped,frame=self.frame(1.1))
            self.result(1.1,('carrier','linear'))
            self.poll(1.4,state=self.stopped,frame=self.frame(1.3))
            self.result(1.3,('carrier','linear'))
            self.poll(1.6,state=self.stopped,frame=self.frame(1.5))
            self.assertTrue(self.pipeline.complete)
            self.assertEqual(decode.call_count,2)
            self.assertEqual(self.pipeline.worker.submissions,[1.1,1.3])
            self.assertEqual(self.pipeline.worker.active,set())
    def test_context_change_waits_for_old_native_call_before_decoding(self):
        self.poll(1);self.poll(1.2,frame=self.frame(1.1))
        self.pipeline.worker.accepting_frame=False
        self.current['pallet']={'pose':[4,5,6]}
        with patch.object(cv2,'imdecode',wraps=cv2.imdecode) as decode:
            self.poll(1.4,frame=self.frame(1.3));decode.assert_not_called()
            self.pipeline.worker.accepting_frame=True
            self.poll(1.6,frame=self.frame(1.5));decode.assert_called_once()
        self.assertEqual(len(Worker.instances),1)
        self.assertEqual(self.pipeline.worker.submissions,[1.1,1.5])
    def test_failed_submission_is_retried_without_marking_frame_consumed(self):
        self.poll(1)
        with patch.object(self.pipeline.worker,'submit',return_value=False):
            self.poll(1.2,frame=self.frame(1.1))
        self.assertIsNone(self.pipeline.inflight);self.assertIsNone(self.pipeline.submitted)
        self.poll(1.3,frame=self.frame(1.1))
        self.assertEqual(self.pipeline.worker.submissions,[1.1])
    def test_capture_before_target_readiness_is_not_decoded_or_classified(self):
        with patch.object(cv2,'imdecode',wraps=cv2.imdecode) as decode:
            self.poll(1,frame=self.frame(.99))
            decode.assert_not_called();self.assertEqual(self.pipeline.worker.submissions,[])
            self.poll(1.2,frame=self.frame(1.1))
            decode.assert_called_once();self.assertEqual(self.pipeline.worker.submissions,[1.1])


class ParallelInspectorTests(unittest.TestCase):
    def test_geometry_and_ai_overlap_on_identical_frame_and_merge_once(self):
        rendezvous=threading.Barrier(2);seen=[]
        class Geometry:
            def inspect(self,frame,anchors,profile):
                seen.append(('geometry',id(frame)));rendezvous.wait(timeout=2)
                return [{'id':'a'}]
        class Provider:
            def predict(self,frame,settings):
                seen.append(('ai',id(frame)));rendezvous.wait(timeout=2);return []
            def close(self):pass
        inspector=ModelInspector(Geometry(),Provider());self.addCleanup(inspector.close)
        frame=np.zeros((2,2,3),np.uint8)
        with patch('so101_teach.ai_inspection.inspection_settings',return_value={'method':'model_roi','confidence':.5}),patch(
                'so101_teach.ai_inspection.combine',side_effect=lambda rows,*args,**kwargs:rows) as combine:
            result=inspector.inspect(frame,[{'id':'a'}],{})
        self.assertEqual([r['id'] for r in result],['a'])
        self.assertEqual({i for _,i in seen},{id(frame)});combine.assert_called_once()
    def test_geometry_error_drains_ai_before_return(self):
        finished=threading.Event()
        class Geometry:
            def inspect(self,*args):raise ValueError('geometry failed')
        class Provider:
            def predict(self,*args):finished.set();return []
            def close(self):pass
        inspector=ModelInspector(Geometry(),Provider());self.addCleanup(inspector.close)
        with patch('so101_teach.ai_inspection.inspection_settings',return_value={'method':'model_roi','confidence':.5}):
            with self.assertRaisesRegex(ValueError,'geometry failed'):inspector.inspect(np.zeros((2,2,3)),[{}],{})
        self.assertTrue(finished.is_set())
    def test_geometry_warmup_never_calls_classification_or_ai(self):
        calls=[]
        class Geometry:
            def projected(self,*args):calls.append(args)
            def inspect(self,*args):raise AssertionError('no classification during warmup')
        class Provider:
            def predict(self,*args):raise AssertionError('no AI during geometry warmup')
            def close(self):pass
        inspector=ModelInspector(Geometry(),Provider());self.addCleanup(inspector.close)
        inspector.prepare_geometry([{'id':'a'}],{},(480,640,3))
        self.assertEqual(len(calls),1)


class RunnerPipelineTests(unittest.TestCase):
    def exercise(self,*,resume=False,fail=False,camera_delay=0.,state_after_camera=None):
        import tempfile
        from pathlib import Path
        from tests.test_episode_cli import cli,FakeLink
        now=[100.];events=[];steps=[step('carrier'),step('finished','finished'),step('linear','linear')]
        self.last_events=events
        probe=startup.StartupInspection(steps,{},None,None,None,lambda _:None)
        carrier=probe.dependencies['carrier'];finished=probe.dependencies['finished']
        class Linear:
            error=None
            def call(self,*args,**kwargs):
                return {'phase':'MOVING' if now[0]<102 else 'TIMED_COMPLETE',
                        'target_mm':100.,'remaining_s':max(0.,102-now[0])}
        encoded=base64.b64encode(cv2.imencode('.jpg',np.zeros((10,10,3),np.uint8))[1]).decode()
        class Link(FakeLink):
            def post(self,path,body=None):
                if path!='/camera':return super().post(path,body)
                now[0]+=camera_delay
                if state_after_camera=='stale':self.stale=True
                elif state_after_camera:self.mode=state_after_camera
                found={}
                for jid,at in ((carrier,101),(finished,104)):
                    if now[0]>=at:found[jid]={'selected':{'metric':{'center_xy_mm':[1,2],'yaw_deg':0}},
                        'pose_measured_at':now[0]}
                return {'at':now[0],'preview_at':now[0],'preview_image':encoded,'detection':{'by_jig':found}}
        class ImmediateWorker(Worker):
            def submit(self,frame,at):
                if fail:
                    self.result={'error':'missing'};return
                self.result={'at':at,'results':{s['id']:{'at':at,'row':row(s['inspection']['station'])}
                    for s in self.steps if s['id'] in self.active}}
        link=Link();link.mode='HOLD'
        run=cli.Runner(link,lambda e:events.append((now[0],e)),cli.Control(),100,linear=Linear())
        run.linear_active=True;run.linear_target=100.;run.linear_until=102.;run.state()
        run.inspection_records=[];run.inspection_episode='test';run.inspection_product='A'
        original_check=run.check;did_resume=[False]
        def check():
            state,_=original_check()
            if resume and run.adopted_jigs is not None and not did_resume[0]:
                did_resume[0]=True;run.preparation_epoch+=1
                return state,True
            return state,False
        bundle={'profile':{},'acquisition_seconds':5,'acquisition_attempts':3,'hold_seconds':5,
                'mesh_hashes':{carrier:'a',finished:'b'}}
        with tempfile.TemporaryDirectory() as directory,patch.object(startup,'FrameInspections',ImmediateWorker),patch(
            'so101_teach.configuration.JigCatalog',return_value=SimpleNamespace(items={})),patch(
            'so101_teach.workcell_placement.load_placement',return_value={'linear_stage':{}}),patch.object(
            cli.time,'monotonic',side_effect=lambda:now[0]),patch.object(
            cli.time,'sleep',side_effect=lambda s:now.__setitem__(0,now[0]+s)),patch.object(run,'check',side_effect=check):
            run.deadline=200.;run.inspection_log=Path(directory)/'inspection.json'
            run.state()
            result=run.prepare_work([],bundle,100.,steps,linear_started=True)
        return events,result,run.inspection_records
    def test_real_runner_overlaps_partial_adoption_and_linear_group(self):
        events,result,records=self.exercise()
        first=lambda prefix:next(t for t,e in events if e.startswith(prefix))
        self.assertLess(first('INSPECTION_PROVISIONAL_PASS:carrier'),first('JIG_CONFIRMED:'))
        self.assertLess(first('INSPECTION_PROVISIONAL_PASS:linear'),first('JIG_CONFIRMED:'))
        self.assertGreaterEqual(first('INSPECTION_STARTED:linear'),102.)
        self.assertLess(first('STARTUP_INSPECTION_PASSED'),104.5)
        self.assertEqual(len(records),3);self.assertEqual(len(result),2)
    def test_slow_camera_rechecks_current_hold_instead_of_expiring_old_snapshot(self):
        events,result,records=self.exercise(camera_delay=.7)
        self.assertTrue(any(e=='STARTUP_INSPECTION_PASSED' for _,e in events))
        self.assertEqual(len(records),3);self.assertEqual(len(result),2)
    def test_slow_camera_does_not_relax_stale_or_changed_follower_rejection(self):
        for state in ('stale','MOVING'):
            with self.subTest(state=state),self.assertRaisesRegex(RuntimeError,'모터 상태 갱신 지연' if state=='stale' else '자세 유지 상태 변경'):
                self.exercise(camera_delay=.7,state_after_camera=state)
            self.assertFalse(any(e=='STARTUP_INSPECTION_PASSED' for _,e in self.last_events))
    def test_resume_retains_jigs_and_discards_all_old_provisional_passes(self):
        events,result,records=self.exercise(resume=True)
        self.assertTrue(any(e=='JIG_BASIS_REUSED' for _,e in events))
        self.assertEqual(sum(e=='JIG_DETECTION_STARTED' for _,e in events),1)
        self.assertEqual(len(records),3)
        self.assertEqual(sum(e.startswith('INSPECTION_PASS:') for _,e in events),3)
