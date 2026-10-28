from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest,time
from so101_teach.episode_inspection import inspection_schedule
from so101_teach.episode_inspection_runtime import InspectionBatch
from tests.test_episode_inspection import CHECK,result
from tests.test_episode_cli import cli,FakeLink,FakeLinear

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.now=10.;p=patch('time.monotonic',lambda:self.now);p.start();self.addCleanup(p.stop)
        self.workers=[];self.passed=Mock()
        def make(step):
            w=SimpleNamespace(result=None,close=Mock(),submit=Mock());self.workers.append(w);return w
        self.batch=InspectionBatch('A',make,self.passed);self.addCleanup(self.batch.close)
        self.step={'id':'a','name':'놓기','inspection':CHECK}
    def test_schedule_starts_after_its_own_step_and_allows_last_step(self):
        steps=[self.step,{'id':'b'},dict(self.step,id='c'),{'id':'d'}];before=deepcopy(steps)
        self.assertEqual([n for n,s in inspection_schedule(steps)],[1,3]);self.assertEqual(steps,before)
        self.assertEqual(inspection_schedule([self.step])[0][0],1)
    def capture_pair(self):
        import numpy as np
        for at in (10.1,10.2):
            self.now=at;self.batch.poll((np.zeros((2,2,3)),at))
    def test_pre_retreat_images_and_duplicate_frames_never_pass(self):
        self.batch.start(self.step);w=self.workers[0];w.result=result(9.9)
        self.capture_pair();self.passed.assert_not_called()
        self.now=11.;w.result=result(10.1);self.batch.poll();self.batch.poll();self.passed.assert_not_called()
        self.now=12.;w.result=result(10.2);self.batch.poll()
        self.assertEqual(self.passed.call_args.args[0]['id'],self.step['id'])
    def test_two_overlapping_checks_complete_independently(self):
        self.batch.start(self.step);self.batch.start(dict(self.step,id='b'));self.capture_pair()
        for at in (10.1,10.2):
            self.now+=1.;self.workers[1].result=result(at);self.batch.poll()
        self.assertEqual(len(self.batch.pending),1);self.assertEqual(self.passed.call_args.args[0]['id'],'b')
    def test_failure_in_any_pending_check_propagates(self):
        self.batch.start(self.step);self.batch.start(dict(self.step,id='b'));self.workers[1].result={'error':'vision failed'}
        with self.assertRaisesRegex(ValueError,'vision failed'):self.batch.poll()
    def test_pause_during_capture_does_not_use_new_position_frames(self):
        self.batch.start(self.step)
        with self.assertRaisesRegex(ValueError,'영상 확보 중 중단'):self.batch.restart()
        self.assertEqual(len(self.workers),1)
    def test_pi_failure_during_motion_sends_hold_before_cleanup(self):
        import tempfile
        from pathlib import Path
        link=FakeLink();events=[];run=cli.Runner(link,events.append,cli.Control(),20,linear=FakeLinear())
        episode={'id':'e','product_type':'A','steps':[dict(self.step,ticks={}),{'id':'r','name':'retreat','ticks':{}},{'id':'n','name':'next','ticks':{}}]}
        def begin(*args):
            link.auto_finish=False
            run.inspection_batch=SimpleNamespace(pending=[self.step],close=lambda:events.append('cleanup'))
        def fail():
            self.assertEqual(link.mode,'MOVING');raise ValueError('camera failed')
        with tempfile.TemporaryDirectory() as temp,patch.object(cli,'ROOT',Path(temp)),patch.object(run,'begin_inspections',side_effect=begin),patch.object(run,'poll_inspections',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'camera failed'):run.execute(episode,{'profile':{}},400,100.)
        self.assertEqual(link.mode,'HOLD');self.assertNotIn('DONE',events);self.assertNotIn('EPISODE_DONE',events);self.assertIn('cleanup',events)
        self.assertEqual([a['action'] for m,a in link.calls if m=='command'][-1],'hold')

class PreparationTests(unittest.TestCase):
    def test_ready_checks_overlap_detection_and_linear_arrival(self):
        from tests.test_startup_pipeline import RunnerPipelineTests
        events,_,_=RunnerPipelineTests().exercise()
        when=lambda prefix:next(t for t,e in events if e.startswith(prefix))
        self.assertLess(when('INSPECTION_PROVISIONAL_PASS:carrier'),when('JIG_CONFIRMED:'))
        self.assertGreaterEqual(when('INSPECTION_STARTED:linear'),102.)
        self.assertLess(when('INSPECTION_PROVISIONAL_PASS:linear'),when('WORK_READY:'))
    def test_start_failure_never_reaches_linear_station_check_or_work_ready(self):
        from tests.test_startup_pipeline import RunnerPipelineTests
        fixture=RunnerPipelineTests()
        with self.assertRaisesRegex(RuntimeError,'missing'):fixture.exercise(fail=True)
        self.assertFalse(any(s.startswith(('WORK_READY','INSPECTION_STARTED:linear')) for _,s in fixture.last_events))

class CheckpointFrameQueueTests(unittest.TestCase):
    def worker(self,**options):
        from so101_teach.episode_inspection_runtime import FrameInspection
        with patch('threading.Thread.start'):
            return FrameInspection('.',{},SimpleNamespace(items={}),{},CHECK,'A',fixed_jigs={},**options)
    def test_next_motion_frame_cannot_replace_pending_checkpoint_image(self):
        import numpy as np
        worker=self.worker(preserve_pending_frames=True)
        original=np.full((2,2,3),20,dtype=np.uint8)
        self.assertTrue(worker.submit(original,10.1));original[:]=99
        # The arm has re-entered the ROI while the first image is waiting.
        self.assertFalse(worker.submit(np.full_like(original,200),11.4))
        frame,at,_=worker.jobs.get_nowait()
        self.assertEqual(at,10.1);self.assertTrue((frame==20).all())
        self.assertTrue(worker.submit(original,11.5))
    def test_preview_and_startup_workers_still_replace_pending_image(self):
        import numpy as np
        worker=self.worker()
        worker.submit(np.zeros((2,2,3),dtype=np.uint8),10.1)
        worker.submit(np.ones((2,2,3),dtype=np.uint8),11.4)
        frame,at,_=worker.jobs.get_nowait()
        self.assertEqual(at,11.4);self.assertTrue((frame==1).all())
