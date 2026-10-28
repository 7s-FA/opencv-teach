"""Exercise real pause/resume control flow with frozen frames and fake motors."""
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
import numpy as np
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach.episode_inspection_runtime import InspectionBatch

class PausedInspectionTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.resume_at=120.;self.quality='normal';self.scheduled=None;self.sent=[];self.pause_once=False
        self.reset_at=None;self.stall=False;self.worker_error=None;self.stop_again_at=None;self.play_times=[]
        self.timer=patch('time.monotonic',lambda:self.now);self.timer.start();self.addCleanup(self.timer.stop)
        self.sleeper=patch('time.sleep',self.advance);self.sleeper.start();self.addCleanup(self.sleeper.stop)
        self.worker=SimpleNamespace(result=None,submit=self.submit,close=Mock())
        self.passed=Mock();self.batch=InspectionBatch('A',lambda step:self.worker,self.passed);self.addCleanup(self.batch.close)
        check={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':10.}
        self.step={'id':'s','name':'검사','inspection':check}
        self.batch.start(self.step)
        for at in (100.1,100.2):self.now=at;self.batch.poll((np.zeros((2,2,3)),at))
        self.link=FakeLink();self.link.mode='HOLD';self.link.auto_finish=False
        original=self.link.rpc
        def rpc(method,args=None):
            if method=='command' and args['action']=='play':self.play_times.append(self.now)
            return original(method,args)
        self.link.rpc=rpc
        self.events=[];self.control=cli.Control();self.linear=FakeLinear()
        self.run=cli.Runner(self.link,self.events.append,self.control,60,linear=self.linear)
        self.run.inspection_batch=self.batch
        self.run.start_finished_return=Mock(side_effect=AssertionError('No new linear return during pause'))
        self.complete=patch.object(self.control,'complete',wraps=self.control.complete);self.completed=self.complete.start();self.addCleanup(self.complete.stop)
    def submit(self,frame,at):
        self.sent.append(at);self.scheduled=(at,self.now+1.);return True
    def advance(self,seconds):
        self.now+=seconds
        if self.now>140:raise AssertionError('pause loop did not finish')
        if self.scheduled and not self.stall and self.now>=self.scheduled[1]:
            at,done=self.scheduled;self.scheduled=None
            self.worker.result=({'error':self.worker_error} if self.worker_error else
                {'at':at,'completed':done,'row':{'quality':self.quality,'state':'housing_seated','product':'A','product_certain':True}})
        if self.reset_at is not None and self.now>=self.reset_at:self.control.reset.set()
        if self.now>=self.resume_at:
            self.control.resume.set();self.link.auto_finish=True
        if self.stop_again_at is not None and self.now>=self.stop_again_at:
            self.control.command('estop');self.stop_again_at=None;self.resume_at=120.
    def poll_motion(self):
        self.batch.poll()
        if not self.pause_once:
            self.pause_once=True;self.link.index=1;self.control.pause.set()
    def move(self):
        with patch.object(self.run,'poll_inspections',side_effect=self.poll_motion):
            return self.run._move('play',[{'q':1},{'q':2}])
    def plays(self):return [args['targets'] for method,args in self.link.calls if method=='command' and args['action']=='play']
    def reset_after_recoverable_failure(self):
        # This fixture exercises frozen-frame draining, then explicitly resets
        # the new retryable checkpoint instead of expecting the old fatal exit.
        self.run.save_failure_images=Mock();self.control.save_receipts=Mock();self.recovery_reason=None
        def emit(event):
            self.events.append(event)
            if event.startswith('RECOVERY_REQUIRED:'):self.recovery_reason=event
            if event=='PAUSED' and self.control.recovery is not None:self.control.command('reset')
        self.run.emit=emit
    def test_long_pause_processes_both_frames_before_operator_resumes(self):
        self.move()
        self.assertEqual(self.sent,[100.1,100.2]);self.passed.assert_called_once()
        self.assertGreaterEqual(self.now,120.);self.assertFalse(self.batch.pending)
        self.assertEqual(self.plays(),[[{'q':1},{'q':2}],[{'q':2}]])
        self.assertLess(self.passed.call_args.args[0]['inspection_capture']['processed_at'],110.2)
        self.run.start_finished_return.assert_not_called()
    def test_early_restart_waits_for_failed_inspection_without_resuming_motion(self):
        self.resume_at=100.3;self.quality='outside'
        self.reset_after_recoverable_failure()
        with self.assertRaises(cli.ResetRequested):self.move()
        self.assertIn('불합격',self.recovery_reason)
        self.assertEqual(self.sent,[100.1,100.2]);self.assertEqual(len(self.plays()),1)
        self.assertNotIn('RESUMED',self.events)
        self.assertNotIn(('restart',),[call.args for call in self.completed.call_args_list])
    def test_early_restart_waits_for_all_results_then_resumes_once(self):
        self.resume_at=100.3;self.move()
        self.passed.assert_called_once();self.assertEqual(len(self.plays()),2)
        self.assertGreaterEqual(self.play_times[1],self.passed.call_args.args[0]['inspection_capture']['processed_at'])
        self.assertEqual(self.events.count('RESUMED'),1)
    def test_worker_error_blocks_restart_and_preserves_failure_evidence(self):
        self.resume_at=100.3;self.worker_error='classifier failed'
        with self.assertRaisesRegex(ValueError,'classifier failed'):self.move()
        self.assertEqual(len(self.plays()),1)
        self.assertEqual(self.run.failure_inspection['result']['error'],'classifier failed')
    def test_real_processing_stall_still_times_out_while_paused(self):
        self.stall=True
        self.reset_after_recoverable_failure()
        with self.assertRaises(cli.ResetRequested):self.move()
        self.assertIn('영상 처리 시간 초과',self.recovery_reason)
        self.assertLess(self.now,111.);self.assertEqual(len(self.plays()),1)
    def test_reset_remains_available_while_waiting_for_classification(self):
        self.stall=True;self.reset_at=101.;self.resume_at=139.
        with self.assertRaises(cli.ResetRequested):self.move()
        self.assertEqual(len(self.plays()),1);self.assertLess(self.now,102.)
    def test_reset_pending_before_pause_has_priority_over_failed_classifier(self):
        self.control.pause.set();self.control.reset.set();self.worker.result={'error':'classifier failed'}
        with self.assertRaises(cli.ResetRequested):self.run.await_pause()
        self.assertEqual(self.plays(),[])
    def test_new_stop_cancels_queued_restart_until_a_new_request(self):
        self.resume_at=100.3;self.stop_again_at=100.5;self.move()
        self.assertGreaterEqual(self.play_times[1],120.);self.assertEqual(len(self.plays()),2)
    def test_final_hold_can_process_frames_without_starting_any_motion(self):
        self.control.pause.set();self.run.await_pause()
        self.assertEqual(self.plays(),[]);self.passed.assert_called_once()
        self.run.start_finished_return.assert_not_called()
    def test_partial_capture_is_not_extended_during_pause(self):
        self.batch.close();self.sent.clear();self.scheduled=None;self.worker.result=None
        self.batch=InspectionBatch('A',lambda step:self.worker,self.passed);self.addCleanup(self.batch.close)
        self.batch.start(self.step);self.now+=.01;self.batch.poll((np.zeros((2,2,3)),self.now))
        self.run.inspection_batch=self.batch;self.control.pause.set()
        with self.assertRaisesRegex(ValueError,'영상 확보 중 중단'):self.run.await_pause()
        self.assertGreaterEqual(self.now,120.);self.assertEqual(self.sent,[]);self.assertEqual(self.plays(),[])
    def test_move_boundary_rechecks_failure_even_if_pause_handler_returns_early(self):
        self.quality='outside';self.now=101.2
        self.worker.result={'at':100.1,'completed':101.2,'row':{'quality':'outside','state':'housing_seated','product':'A','product_certain':True}}
        self.batch.poll()
        def paused():
            self.now=103.;self.worker.result={'at':100.2,'completed':102.2,'row':{'quality':'outside','state':'housing_seated','product':'A','product_certain':True}}
            self.control.pause.clear();self.link.mode='HOLD';self.link.pending=False;self.link.index=1
            return self.link.state()
        self.reset_after_recoverable_failure()
        with patch.object(self.run,'await_pause',side_effect=paused):
            with self.assertRaises(cli.ResetRequested):self.move()
        self.assertIn('불합격',self.recovery_reason)
        self.assertEqual(len(self.plays()),1)
