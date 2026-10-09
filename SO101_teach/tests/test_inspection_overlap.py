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
    def test_pre_retreat_images_and_duplicate_frames_never_pass(self):
        self.batch.start(self.step);w=self.workers[0];w.result=result(9.9);self.now=10.2;self.batch.poll();self.passed.assert_not_called()
        w.result=result(10.1);self.batch.poll();self.batch.poll();self.passed.assert_not_called()
        self.now=10.5;w.result=result(10.4);self.batch.poll();self.passed.assert_called_once_with(self.step)
    def test_two_overlapping_checks_complete_independently(self):
        self.batch.start(self.step);self.batch.start(dict(self.step,id='b'))
        for at in (10.1,10.3):
            self.now=at+.01;self.workers[1].result=result(at);self.batch.poll()
        self.assertEqual(len(self.batch.pending),1);self.assertEqual(self.passed.call_args.args[0]['id'],'b')
    def test_failure_in_any_pending_check_propagates(self):
        self.batch.start(self.step);self.batch.start(dict(self.step,id='b'));self.workers[1].result={'error':'vision failed'}
        with self.assertRaisesRegex(ValueError,'vision failed'):self.batch.poll()
    def test_pause_discards_worker_and_restarts_fresh_frame_budget(self):
        self.batch.start(self.step);self.workers[0].result=result(10.1);self.now=10.2;self.batch.poll()
        self.now=20.;self.batch.restart();self.workers[0].close.assert_called_once();self.workers[1].result=result(10.3);self.batch.poll();self.passed.assert_not_called()
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
            with self.assertRaisesRegex(ValueError,'camera failed'):run.execute(episode,{},400,100.)
        self.assertEqual(link.mode,'HOLD');self.assertNotIn('DONE',events);self.assertNotIn('EPISODE_DONE',events);self.assertIn('cleanup',events)
        self.assertEqual([a['action'] for m,a in link.calls if m=='command'][-1],'hold')

class PreparationTests(unittest.TestCase):
    def test_linear_and_detection_then_fixed_check_then_linear_check_then_motion_ready(self):
        events=[];run=cli.Runner(FakeLink(),events.append,cli.Control(),20,linear=FakeLinear())
        checks=[{'inspection':{'station':'linear'}},{'inspection':{'station':'carrier'}}]
        with patch.object(run,'start_linear',side_effect=lambda t:(events.append('linear'),setattr(run,'linear_active',True))),patch.object(run,'poll_linear',return_value=False),patch.object(run,'measure',side_effect=lambda *a:events.append('jig') or {}),patch.object(run,'wait_linear',side_effect=lambda:events.append('linear-stop') or False),patch.object(run,'inspect_startup_group',side_effect=lambda steps,*a:events.extend(s['inspection']['station'] for s in steps)):
            run.prepare_work([],{},100,checks)
        self.assertLess(events.index('linear'),events.index('jig'));self.assertLess(events.index('jig'),events.index('carrier'));self.assertLess(events.index('carrier'),events.index('linear-stop'));self.assertLess(events.index('linear-stop'),events.index('linear',1))
    def test_start_failure_never_reaches_linear_station_check_or_work_ready(self):
        events=[];run=cli.Runner(FakeLink(),events.append,cli.Control(),20,linear=FakeLinear())
        with patch.object(run,'measure',return_value={}),patch.object(run,'inspect_startup_group',side_effect=ValueError('missing')),patch.object(run,'wait_linear') as wait:
            with self.assertRaisesRegex(ValueError,'missing'):run.prepare_work([],{},100,[{'inspection':{'station':'carrier'}}])
            wait.assert_not_called()
        self.assertFalse(any(s.startswith('WORK_READY') for s in events))
