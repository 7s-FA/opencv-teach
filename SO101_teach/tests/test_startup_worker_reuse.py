"""Exercise context replacement while native classification is in flight."""
import threading
import time
import unittest
import queue
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np

from so101_teach.episode_inspection_runtime import FrameInspections
from tests.test_startup_pipeline import step,row


class WorkerReuseTests(unittest.TestCase):
    def wait_until(self,predicate):
        deadline=time.monotonic()+3
        while not predicate():
            if time.monotonic()>deadline:self.fail('worker did not finish')
            time.sleep(.005)

    def worker(self,*,fail_first=False):
        entered=threading.Event();release=threading.Event();seen=[]
        self.addCleanup(release.set)
        def classify(frame,anchors,profile,**kwargs):
            seen.append((int(frame[0,0,0]),anchors,kwargs['context_anchors']))
            if len(seen)==1:
                entered.set()
                if not release.wait(3):raise RuntimeError('test inference blocked')
                if fail_first:raise ValueError('invalid old geometry')
            return [{'id':'housing',**row()}]
        inspector=SimpleNamespace(inspect=Mock(side_effect=classify),close=Mock(),stop=None)
        def context(frame,fresh,fixed,items,profile,reference,placement):
            anchor={'id':'housing','part':'housing','product':'A','inspection_allowed':True}
            return {'carrier':([anchor],'ready')},[{'pose':fixed['pose'],'position':placement['position']}]
        for target,value in (
            ('so101_teach.ai_inspection.inspector_for',inspector),
            ('so101_teach.episode_inspection.fixed_jig_results',lambda value:value),
            ('so101_teach.inspection_geometry.inspection_context',context),
        ):
            patcher=patch(target,side_effect=value) if callable(value) else patch(target,return_value=value)
            patcher.start();self.addCleanup(patcher.stop)
        worker=FrameInspections('.',{},SimpleNamespace(items={}),{'position':1},[step('carrier')],fixed_jigs={'pose':1})
        def close():
            release.set();worker.close();worker.join(3)
            self.assertFalse(worker.is_alive())
        self.addCleanup(close)
        worker.publish_visual=Mock()
        return worker,inspector,entered,release,seen

    def test_context_change_drops_inflight_and_queued_old_results(self):
        worker,inspector,entered,release,seen=self.worker()
        frame=np.ones((4,4,3),np.uint8)
        worker.submit(frame,10.1);self.assertTrue(entered.wait(2))
        self.assertFalse(worker.accepting_frame)
        worker.submit(frame*2,10.2)
        worker.update_context({'pose':2},{'position':2},['carrier'])
        self.assertTrue(worker.jobs.empty());self.assertIsNone(worker.result)
        release.set();self.wait_until(lambda:worker.accepting_frame)
        self.assertIsNone(worker.result);worker.publish_visual.assert_not_called()
        worker.submit(frame*3,10.3);self.wait_until(lambda:worker.result is not None)
        self.assertEqual(worker.result['at'],10.3)
        self.assertEqual([item[0] for item in seen],[1,3])
        self.assertEqual(seen[0][2],[{'pose':1,'position':1}])
        self.assertEqual(seen[1][2],[{'pose':2,'position':2}])
        self.assertEqual(inspector.inspect.call_count,2)
        inspector.close.assert_not_called();worker.publish_visual.assert_called_once()

    def test_stop_during_native_call_never_publishes_a_late_pass(self):
        worker,inspector,entered,release,seen=self.worker()
        worker.submit(np.ones((4,4,3),np.uint8),10.1);self.assertTrue(entered.wait(2))
        worker.close();release.set();worker.join(3)
        self.assertIsNone(worker.result);worker.publish_visual.assert_not_called()
        self.assertFalse(worker.submit(np.ones((4,4,3),np.uint8),10.2))
        inspector.close.assert_called_once()

    def test_old_context_error_is_discarded_and_current_context_still_runs(self):
        worker,inspector,entered,release,seen=self.worker(fail_first=True)
        frame=np.ones((4,4,3),np.uint8)
        worker.submit(frame,10.1);self.assertTrue(entered.wait(2))
        worker.update_context({'pose':2},{'position':2},['carrier']);release.set()
        self.wait_until(lambda:worker.accepting_frame)
        self.assertIsNone(worker.result);self.assertTrue(worker.is_alive())
        worker.submit(frame,10.2);self.wait_until(lambda:worker.result is not None)
        self.assertEqual(worker.result['at'],10.2);self.assertNotIn('error',worker.result)

    def test_current_context_error_still_terminates_and_blocks_reuse(self):
        worker,inspector,entered,release,seen=self.worker(fail_first=True)
        worker.submit(np.ones((4,4,3),np.uint8),10.1);self.assertTrue(entered.wait(2))
        release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(worker.result,{'error':'invalid old geometry'})
        with self.assertRaisesRegex(RuntimeError,'invalid old geometry'):
            worker.update_context({'pose':2},{'position':2},['carrier'])

    def test_removed_targets_do_not_start_another_classification(self):
        worker,inspector,entered,release,seen=self.worker()
        worker.submit(np.ones((4,4,3),np.uint8),10.1);self.assertTrue(entered.wait(2))
        worker.update_context({'pose':1},{'position':2},[])
        release.set();self.wait_until(lambda:worker.accepting_frame)
        worker.submit(np.ones((4,4,3),np.uint8),10.2)
        self.wait_until(lambda:worker.accepting_frame)
        self.assertIsNone(worker.result);self.assertEqual(inspector.inspect.call_count,1)

    def test_result_error_cannot_be_cleared_by_context_change(self):
        worker,inspector,entered,release,seen=self.worker()
        worker.result={'error':'model failed'}
        with self.assertRaisesRegex(RuntimeError,'model failed'):
            worker.update_context({'pose':2},{'position':2},['carrier'])

    def test_dynamic_detection_cannot_be_switched_to_fixed_geometry(self):
        with patch('threading.Thread.start'):
            worker=FrameInspections('.',{},SimpleNamespace(items={}),{},[step('carrier')])
        with self.assertRaisesRegex(ValueError,'고정 지그'):
            worker.update_context({}, {}, ['carrier'])

    def test_close_wakes_idle_queue_without_waiting_for_poll_timeout(self):
        waiting=threading.Event();original_queue=queue.Queue
        class SlowQueue(original_queue):
            def get(self,block=True,timeout=None):
                if block:
                    waiting.set();return super().get(timeout=2)
                return super().get(block=False)
        with patch('so101_teach.episode_inspection_runtime.queue.Queue',SlowQueue):
            worker,_,_,_,_=self.worker()
        self.assertTrue(waiting.wait(2))
        worker.close()
        self.assertFalse(worker.is_alive())
