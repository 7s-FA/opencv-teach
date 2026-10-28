"""Offline profiling invariants: timing must not change evidence or work."""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np
from so101_teach.preparation_profile import PreparationProfile,timed


class ProfileTests(unittest.TestCase):
    def test_nested_measurements_preserve_values_and_exception(self):
        now=[10.];p=PreparationProfile(lambda:now[0]);failure=ValueError('original')
        def inner(*,profile):
            now[0]+=2;self.assertEqual(profile,'argument');raise failure
        with self.assertRaises(ValueError) as caught:
            with p.measure('outer'):
                now[0]+=1;p.call('inner',inner,profile='argument')
        self.assertIs(caught.exception,failure)
        self.assertEqual(p.snapshot()['metrics']['outer']['total_seconds'],3)
        self.assertEqual(p.snapshot()['metrics']['inner']['first_started_seconds'],1)
        value=object();self.assertIs(timed(None,'none',lambda:value),value)
        self.assertIs(timed(p,'return',lambda:value),value)

    def test_overlapping_same_name_keeps_earliest_start(self):
        now=[0.];p=PreparationProfile(lambda:now[0])
        with p.measure('overlap'):
            now[0]=1.
            with p.measure('overlap'):now[0]=2.
            now[0]=3.
        row=p.snapshot()['metrics']['overlap']
        self.assertEqual(row['first_started_seconds'],0.)
        self.assertEqual(row['last_finished_seconds'],3.)

    def test_samples_deduplicate_and_reject_invalid_durations(self):
        p=PreparationProfile()
        for _ in range(3):p.sample('jig',(1,0,1),{'ai':.5,'bad':float('nan'),'bool':True,'negative':-1})
        p.sample('jig',(2,0,2),{'ai':.2})
        self.assertEqual(p.snapshot()['metrics']['jig.ai']['count'],2)
        self.assertEqual(set(p.snapshot()['metrics']),{'jig.ai'})
        for i in range(1000):p.sample('source'+str(i),i,{'value':1});p.mark(str(i))
        self.assertLessEqual(len(p.metrics),256);self.assertLessEqual(len(p.seen),128)
        self.assertLessEqual(len(p.milestones),256)

    def test_repeated_frame_after_another_frame_is_not_counted_twice(self):
        p=PreparationProfile()
        for token in ('frame-a','frame-b','frame-a'):p.sample('camera',token,{'ai':1.})
        self.assertEqual(p.snapshot()['metrics']['camera.ai']['count'],2)

    def test_cumulative_snapshots_keep_interval_identity_and_report_retention(self):
        now=[0.];p=PreparationProfile(lambda:now[0])
        with p.measure('first'):now[0]=2.
        first=p.snapshot()
        with p.measure('second'):now[0]=3.
        second=p.snapshot()
        self.assertEqual(first['profile_id'],second['profile_id'])
        self.assertEqual(first['intervals'][0],second['intervals'][0])
        self.assertEqual(len(first['intervals']),1);self.assertEqual(len(second['intervals']),2)
        for _ in range(4096):
            with p.measure('many'):now[0]+=.001
        result=p.snapshot();self.assertEqual(len(result['intervals']),4096)
        self.assertEqual(result['dropped_intervals'],2)

    def test_concurrent_samples_and_snapshot_are_consistent(self):
        p=PreparationProfile()
        def write():
            for _ in range(1000):p.add('shared',.001)
        threads=[threading.Thread(target=write) for _ in range(4)]
        for t in threads:t.start()
        for t in threads:t.join()
        snapshot=p.snapshot();self.assertEqual(snapshot['metrics']['shared']['count'],4000)
        self.assertEqual(snapshot['metrics']['shared']['total_seconds'],4.)
        snapshot['metrics']['shared']['count']=0
        self.assertEqual(p.snapshot()['metrics']['shared']['count'],4000)

    def test_rpc_finish_and_cleanup_are_distinct(self):
        # Deterministic parallel timeline: planning finishes during inspection.
        from concurrent.futures import ThreadPoolExecutor
        now=[0.];p=PreparationProfile(lambda:now[0]);done=threading.Event()
        def rpc():now[0]=1.;return 'plan'
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(p.call,'arm2.planning.rpc',rpc)
            future.add_done_callback(lambda _:done.set());self.assertTrue(done.wait(2))
            now[0]=5.;self.assertEqual(future.result(),'plan')
            with p.measure('arm2.inspection.close'):now[0]=6.
        rows=p.snapshot()['metrics']
        self.assertEqual(rows['arm2.planning.rpc']['last_finished_seconds'],1.)
        self.assertEqual(rows['arm2.inspection.close']['first_started_seconds'],5.)


class ConsumerTests(unittest.TestCase):
    def runner(self):
        from tests.test_episode_cli import cli,FakeLink
        run=cli.Runner(FakeLink(),Mock(),cli.Control(),100)
        run.camera_prepare_started=100.;return run

    def test_warmup_kept_separate_from_detection_and_duplicate_polls(self):
        run=self.runner();warm={'started':101.,'completed':103.,'seconds':2.}
        ready={'at':104.,'detection':{'vision_ready':True,'processing_seconds':.001,'model_preparation_timing':warm}}
        with patch.object(run.link,'post',return_value=ready):
            self.assertIs(run.camera_frame({}),ready);run.camera_frame({})
        frame={'at':105.,'detection':{'processing_seconds':.4,'model_seconds':.1,'opencv_seconds':.2}}
        with patch.object(run.link,'post',return_value=frame):run.camera_frame({});run.camera_frame({})
        m=run.timing.snapshot()['metrics']
        self.assertEqual(m['arm2.camera.model_warmup']['total_seconds'],2.)
        self.assertEqual(m['arm2.camera.model_warmup']['count'],1)
        self.assertEqual(m['arm2.jig.processing_seconds']['count'],1)
        self.assertEqual(m['arm2.jig.processing_seconds']['total_seconds'],.4)

    def test_previous_job_warmup_is_not_attributed_to_new_job(self):
        run=self.runner()
        with patch.object(run.link,'post',return_value={'at':101.,'detection':{'vision_ready':True,
                'model_preparation_timing':{'started':10.,'seconds':3.}}}):run.camera_frame({})
        self.assertNotIn('arm2.camera.model_warmup',run.timing.snapshot()['metrics'])

    def test_reports_keep_current_arm_shared_loading_and_milestones(self):
        run=self.runner()
        for name in ('arm2.jig.ai','arm3.jig.ai','arm3.load.total','shared.open_links'):run.timing.add(name,1)
        run.timing.mark('arm2.inspection.passed.a');run.timing.mark('arm3.inspection.passed.b')
        run.report_preparation();payload=json.loads(run.emit.call_args.args[0].split(':',1)[1])
        self.assertEqual(set(payload['metrics']),{'arm2.jig.ai','arm3.load.total','shared.open_links'})
        self.assertEqual(set(payload['milestones']),{'arm2.inspection.passed.a'})
        from integration.episode_timing import preparation_lines
        lines=preparation_lines(payload,'arm3')
        self.assertTrue(any(line.startswith('설정 로딩') and '1.000' in line and line.rstrip().endswith('1') for line in lines))
        self.assertTrue(any('합산하지 않음' in line for line in lines))
        from tests.test_work_monitor import monitor
        event={'at':1.,'arm':'arm2','status':'A_PREPARATION_PROFILE:'+json.dumps(payload)}
        self.assertIn('준비 상세 계측',monitor.visible_event(event)['status'])
        invalid={**event,'status':'A_PREPARATION_PROFILE:not-json'}
        self.assertEqual(monitor.visible_event(invalid),invalid)

    def test_prepare_retains_first_warmup_time_without_collecting_poses(self):
        from tests.test_episode_vision_prepare import PreparedProvider,SETTINGS
        from so101_teach.vision_service import MultiDetector
        detector=MultiDetector(SimpleNamespace(items={},revision=0),{'jig_detection':SETTINGS})
        detector.region_model=PreparedProvider();self.addCleanup(detector.close)
        frame=np.zeros((4,4,3),np.uint8)
        first=detector.prepare(frame);second=detector.prepare(frame)
        self.assertTrue(first['model_inference_called']);self.assertFalse(second['model_inference_called'])
        self.assertEqual(first['model_preparation_timing'],second['model_preparation_timing'])
        self.assertEqual(detector.latches['pallet'].stability.samples,[])
        self.assertEqual(detector.region_tracks,{})

    def test_ai_geometry_still_overlap_with_profiling(self):
        from so101_teach.ai_inspection import ModelInspector
        barrier=threading.Barrier(2);p=PreparationProfile();seen=[]
        class Geometry:
            def inspect(self,frame,*args):seen.append(id(frame));barrier.wait(2);return [{'id':'a'}]
        class Provider:
            def predict(self,frame,*args):seen.append(id(frame));barrier.wait(2);return []
            def close(self):pass
        inspector=ModelInspector(Geometry(),Provider());self.addCleanup(inspector.close)
        inspector.timing=p;inspector.timing_prefix='arm2.inspection';frame=np.zeros((4,4,3),np.uint8)
        with patch('so101_teach.ai_inspection.inspection_settings',return_value={'method':'model_roi','confidence':.5}),patch(
                'so101_teach.ai_inspection.combine',side_effect=lambda rows,*args,**kwargs:rows):
            rows=inspector.inspect(frame,[{'id':'a'}],{})
        self.assertEqual(rows[0]['id'],'a');self.assertEqual(seen,[id(frame),id(frame)])
        for key in ('ai','geometry','combine','ai_wait_after_geometry'):
            self.assertEqual(p.snapshot()['metrics']['arm2.inspection.'+key]['count'],1)

    def test_worker_records_queue_time_without_changing_capture_time(self):
        import tempfile
        from tests.test_grouped_inspection import GroupTests
        from so101_teach.episode_inspection_runtime import FrameInspections
        p=PreparationProfile();steps=GroupTests().steps();anchors=[{'id':'linear_0','inspection_allowed':True}]
        detector=Mock();detector.process.return_value={'live_by_jig':{}}
        inspector=Mock();inspector.inspect.return_value=[{'id':'linear_0','quality':'normal'}]
        with tempfile.TemporaryDirectory() as temp,patch('so101_teach.vision_service.MultiDetector',return_value=detector),patch(
                'so101_teach.shape_inspection.ShapeInspector',return_value=inspector),patch(
                'so101_teach.inspection_geometry.anchors_for',return_value=(anchors,'test')):
            worker=FrameInspections(temp,{},SimpleNamespace(items={}),{'linear_stage':{'startup_state':{'known':True}}},steps,timing=p)
            try:
                captured=time.monotonic()-.5;worker.submit(np.zeros((4,4,3),np.uint8),captured)
                until=time.monotonic()+2
                while worker.result is None and time.monotonic()<until:time.sleep(.005)
                self.assertIn('results',worker.result)
                self.assertEqual(worker.result['results']['one']['at'],captured)
                metrics=p.snapshot()['metrics']
                self.assertGreaterEqual(metrics['inspection.frame_age_at_start']['total_seconds'],.5)
                self.assertEqual(metrics['inspection.frame_queue_wait']['count'],1)
            finally:worker.close();worker.join(2)
