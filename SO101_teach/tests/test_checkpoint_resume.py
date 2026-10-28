"""Checkpoint faults retain a job, but never authorize movement without recheck."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock,patch
import numpy as np
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach.episode_faults import InspectionRetry,JigRetry,ResumeBlocked,RetryableFault
from so101_teach.episode_inspection import InspectionDecision
from so101_teach.episode_inspection_runtime import InspectionBatch
from episode_resume import PoseGuard,same_jigs


class FaultClassificationTests(unittest.TestCase):
    def decision(self):return InspectionDecision({'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5},'A',10)
    def test_negative_result_and_timeout_are_retryable_but_worker_error_is_not(self):
        with self.assertRaises(InspectionRetry):self.decision().observe(None,15)
        d=self.decision();bad={'row':{'quality':'abnormal','state':'empty','product':'A','product_certain':True}}
        d.observe({**bad,'at':10.1},10.1)
        with self.assertRaises(InspectionRetry):d.observe({**bad,'at':10.2},10.2)
        with self.assertRaises(ValueError) as caught:self.decision().observe({'error':'model disconnected'},10.1)
        self.assertNotIsInstance(caught.exception,RetryableFault)
    def test_recapture_discards_old_frames_results_and_counts(self):
        workers=[];now=[10.]
        def make(step):
            worker=SimpleNamespace(result=None,submit=Mock(return_value=True),close=Mock());workers.append(worker);return worker
        with patch('time.monotonic',side_effect=lambda:now[0]):
            batch=InspectionBatch('A',make,Mock());step={'id':'s','name':'check','inspection':{'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5}}
            batch.start(step);now[0]=10.1;batch.poll((np.zeros((2,2,3),np.uint8),10.1))
            now[0]=20;batch.recapture();item=batch.pending[0]
            self.assertEqual(item['frames'],[]);self.assertEqual(item['decision'].count,0);self.assertEqual(item['decision'].started,20)
            workers[0].close.assert_called_once();self.assertIsNot(item['worker'],workers[0]);batch.close()


class ResumeStateTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.];self.clock=patch('time.monotonic',side_effect=lambda:self.now[0]);self.clock.start();self.addCleanup(self.clock.stop)
        self.sleep=patch('time.sleep',side_effect=lambda n:self.now.__setitem__(0,self.now[0]+n));self.sleep.start();self.addCleanup(self.sleep.stop)
        self.link=FakeLink();self.link.mode='HOLD';self.control=cli.Control();self.events=[]
        self.runner=cli.Runner(self.link,self.emit,self.control,20,linear=FakeLinear())
        self.runner.save_failure_images=Mock();self.control.save_receipts=Mock();self.on_pause=lambda:None
        self.receipts=[]
    def emit(self,event):
        self.events.append(event)
        if event=='PAUSED':self.on_pause()
    def restart(self,key='a'):
        value={'job_id':self.control.job_id,'request_id':key*32,'command':'restart','stop_generation':self.control.stop_generation}
        result=json.loads(self.control.request(value));self.receipts.append(result['request_id']);return result
    def test_failed_inspection_rechecks_then_completes_restart_without_replaying_motion(self):
        self.on_pause=self.restart;calls=[]
        def operation():
            calls.append('check')
            if len(calls)==1:raise InspectionRetry('part missing')
            self.assertTrue(self.control.paused);self.assertTrue(self.control.rechecking)
            self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'pending')
            self.runner.check();return 'passed'
        validate=Mock()
        self.assertEqual(self.runner.retry_visual(operation,validate=validate),'passed')
        validate.assert_called_once();self.assertEqual(len(calls),2)
        self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'completed')
        self.assertFalse(self.control.pause.is_set());self.assertFalse(self.control.paused);self.assertFalse(self.control.ending)
        self.assertIn('RECOVERY_RECHECK_PASSED',self.events);self.assertEqual(self.events[-1],'RESUMED')
        self.assertFalse(any(m=='command' and a['action'] in ('arm','move','play') for m,a in self.link.calls))
    def test_failed_recheck_stays_paused_and_requires_another_explicit_restart(self):
        self.on_pause=lambda:self.restart('a' if not self.receipts else 'b')
        operation=Mock(side_effect=[InspectionRetry('first'),InspectionRetry('still missing'),'ok'])
        self.assertEqual(self.runner.retry_visual(operation),'ok')
        self.assertEqual([self.control.receipts[k]['state'] for k in self.receipts],['failed','completed'])
        self.assertEqual(self.events.count('PAUSED'),2);self.assertEqual(self.events.count('RESUMED'),1)
    def test_new_stop_cancels_delayed_pass_and_reset_does_not_resume(self):
        def on_pause():
            if not self.receipts:self.restart()
            else:self.control.command('reset')
        self.on_pause=on_pause;calls=[0]
        def operation():
            calls[0]+=1
            if calls[0]==1:raise InspectionRetry('initial')
            self.control.command('estop');return 'late pass'
        with self.assertRaises(cli.ResetRequested):self.runner.retry_visual(operation)
        self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'cancelled')
        self.assertNotIn('RESUMED',self.events);self.assertNotIn('RECOVERY_RECHECK_PASSED',self.events)
        self.assertFalse(self.control.ending)
    def test_new_stop_during_completion_write_cancels_receipt_and_never_moves(self):
        self.on_pause=lambda:self.restart() if not self.receipts else self.control.command('reset')
        stopped=[False]
        def save():
            if any(r['state']=='completed' for r in self.control.receipts.values()) and not stopped[0]:
                stopped[0]=True;self.control.command('estop')
        self.control.save_receipts=save
        operation=Mock(side_effect=[InspectionRetry('initial'),'ok'])
        with self.assertRaises(cli.ResetRequested):self.runner.retry_visual(operation)
        self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'cancelled');self.assertNotIn('RESUMED',self.events)
    def test_cancel_during_completion_write_cannot_publish_a_late_resume(self):
        self.on_pause=self.restart
        def save():
            if any(r['state']=='completed' for r in self.control.receipts.values()):self.control.shutdown.set()
        self.control.save_receipts=save
        with self.assertRaises(cli.ShutdownRequested):self.runner.retry_visual(Mock(side_effect=[InspectionRetry('first'),'ok']))
        self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'cancelled')
        self.assertNotIn('RESUMED',self.events);self.assertNotIn('RECOVERY_RECHECK_PASSED',self.events)
    def test_slow_completion_write_cannot_resume_with_expired_motor_evidence(self):
        self.on_pause=lambda:self.restart() if not self.receipts else self.control.command('reset')
        delayed=[False]
        def save():
            if any(r['state']=='completed' for r in self.control.receipts.values()) and not delayed[0]:
                delayed[0]=True;self.now[0]+=1.
        self.control.save_receipts=save
        with self.assertRaises(cli.ResetRequested):self.runner.retry_visual(Mock(side_effect=[InspectionRetry('first'),'ok']))
        self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'failed')
        self.assertNotIn('RESUMED',self.events)
    def test_changed_configuration_blocks_retry_without_running_inspection(self):
        self.runner.resume_configuration={'version':1};self.runner.configuration_state=Mock(return_value={'version':2})
        self.on_pause=lambda:self.restart() if not self.receipts else self.control.command('reset')
        operation=Mock(side_effect=InspectionRetry('initial'))
        with self.assertRaises(cli.ResetRequested):self.runner.retry_visual(operation)
        operation.assert_called_once();self.assertEqual(self.control.receipts[self.receipts[0]]['state'],'failed')
        self.assertTrue(any('설정이 변경' in e for e in self.events))
    def test_fatal_device_error_during_recheck_is_not_downgraded_to_another_pause(self):
        self.on_pause=self.restart;operation=Mock(side_effect=[InspectionRetry('initial'),RuntimeError('servo lost')])
        with self.assertRaisesRegex(RuntimeError,'servo lost'):self.runner.retry_visual(operation)
        self.assertEqual(self.events.count('PAUSED'),1);self.assertNotIn('RESUMED',self.events)
    def test_wait_does_not_consume_episode_execution_budget(self):
        before=self.runner.deadline
        def waiting():self.now[0]+=600;self.restart()
        self.on_pause=waiting
        self.runner.retry_visual(Mock(side_effect=[JigRetry('no jig'),'ok']))
        self.assertEqual(self.runner.deadline,before+600)
    def test_duplicate_restart_during_recheck_is_rejected(self):
        self.on_pause=self.restart
        def validate():
            with self.assertRaisesRegex(ValueError,'CONTROL_PENDING'):self.control.command('restart')
        self.runner.retry_visual(Mock(side_effect=[InspectionRetry('initial'),'ok']),validate=validate)
    def test_frozen_pose_guard_rejects_moved_arm_and_changed_request(self):
        original=self.link.state();guard=PoseGuard(original)
        for field in ('pose','request'):
            changed=deepcopy(original)
            if field=='pose':changed['latest']['ticks']['0']+=21
            else:changed['request_id']=999
            with self.assertRaises(ResumeBlocked):guard.validate(changed,cli.stopped)
    def test_checkpoint_pause_is_routed_to_fresh_recheck_instead_of_old_batch_restart(self):
        self.runner.inspection_checkpoint=self.link.state();self.control.pause.set()
        with self.assertRaises(InspectionRetry):self.runner.check()
    def test_finished_linear_return_is_deferred_until_retry_passes(self):
        self.runner.recovery=SimpleNamespace(rechecking=True)
        self.runner.start_linear=Mock();self.runner.start_finished_return();self.runner.start_linear.assert_not_called()


class JigResumeTests(unittest.TestCase):
    def test_only_same_geometry_within_current_consensus_tolerance_is_reusable(self):
        old={'j':{'pose':[10,20,359.9],'symmetry_deg':360,'stl_sha256':'mesh'}}
        new=deepcopy(old);new['j']['pose']=[10.1,20.1,.1];self.assertTrue(same_jigs(old,new))
        new['j']['pose'][0]=11;self.assertFalse(same_jigs(old,new))
        new=deepcopy(old);new['j']['stl_sha256']='other';self.assertFalse(same_jigs(old,new))
        self.assertFalse(same_jigs(old,{}))
    def test_nonfinite_jig_coordinates_never_authorize_a_retry(self):
        value={'j':{'pose':[1,2,3],'symmetry_deg':360,'stl_sha256':'mesh'}}
        for bad in (float('nan'),float('inf')):
            changed=deepcopy(value);changed['j']['pose'][0]=bad
            self.assertFalse(same_jigs(value,changed))
    def test_repeated_failure_images_have_distinct_attempt_folders(self):
        from failure_images import FailureImages
        with TemporaryDirectory() as directory:
            cache=FailureImages(Path(directory));paths=[]
            for key in ('a','b'):
                cache.saved=None;paths.append(cache.save('fail',context={'run_id':'c'*32,'recovery_id':key*32}))
            self.assertNotEqual(paths[0],paths[1]);self.assertTrue(all(path.is_file() for path in paths))
            from so101_teach.failure_archive import list_records,read_record
            rows=list_records(directory)['records'];self.assertEqual(len(rows),2)
            self.assertEqual({read_record(directory,row['key'])['record']['context']['recovery_id'] for row in rows},{'a'*32,'b'*32})


class CheckpointFlowTests(unittest.TestCase):
    def exercise(self,second_passes=True):
        import base64,cv2
        now=[100.];events=[];workers=[];passed=[]
        link=FakeLink();link.mode='HOLD';linear=FakeLinear();control=cli.Control()
        receipt_ids=[]
        def emit(event):
            events.append(event)
            if event=='PAUSED':
                if not receipt_ids:
                    key='a'*32;receipt_ids.append(key)
                    control.request({'job_id':control.job_id,'request_id':key,'command':'restart','stop_generation':control.stop_generation})
                else:control.command('reset')
        runner=cli.Runner(link,emit,control,30,linear=linear);runner.save_failure_images=Mock();control.save_receipts=Mock()
        runner.execution_bundle={'profile':{}};runner.inspection_jigs={'pallet':{'pose':[10,20,30],'symmetry_deg':360}};runner.linear_target=100
        runner.measure=Mock(side_effect=AssertionError('Recheck must retain the adopted pallet'))
        check={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5}
        steps=[{'id':'placed','name':'놓기','jig_id':'pallet','inspection':check},{'id':'next','name':'다음 동작'}]
        runner.execution_steps=deepcopy(steps);runner.inspection_after_at=runner.inspection_after_preview=None
        raw=base64.b64encode(cv2.imencode('.jpg',np.zeros((8,12,3),np.uint8))[1]).decode()
        original_post=link.post
        def post(endpoint,body=None):
            if endpoint=='/camera' and not link.preparing:return {'preview_at':now[0],'preview_image':raw}
            return original_post(endpoint,body)
        def make(step):
            index=len(workers);worker=SimpleNamespace(result=None,close=Mock(),captures=[])
            def submit(frame,at):
                worker.captures.append(at)
                good=index>0 and second_passes
                worker.result={'at':at,'completed':now[0],'row':{'quality':'normal' if good else 'abnormal',
                    'state':'housing_seated' if good else 'empty','product':'A','product_certain':True}}
                return True
            worker.submit=submit;workers.append(worker);return worker
        runner.inspection_batch=InspectionBatch('A',make,lambda step:passed.append(step['id']))
        try:
            with patch('time.monotonic',side_effect=lambda:now[0]),patch('time.sleep',side_effect=lambda n:now.__setitem__(0,now[0]+n)),patch.object(link,'post',side_effect=post):
                if second_passes:runner.move('play',[{'target':1},{'target':2}],steps,{})
                else:
                    with self.assertRaises(cli.ResetRequested):runner.move('play',[{'target':1},{'target':2}],steps,{})
            return link,workers,passed,events,control
        finally:runner.inspection_batch.close()
    def test_real_batch_failure_then_new_capture_pass_resumes_only_remaining_segment(self):
        link,workers,passed,events,control=self.exercise()
        plays=[args['targets'] for method,args in link.calls if method=='command' and args['action']=='play']
        self.assertEqual(plays,[[{'target':1}],[{'target':2}]])
        self.assertEqual(passed,['placed']);self.assertEqual(len(workers),2)
        self.assertGreater(min(workers[1].captures),max(workers[0].captures))
        workers[0].close.assert_called_once();self.assertIn('INSPECTION_RECHECK_STARTED',events)
        self.assertEqual(control.receipts['a'*32]['state'],'completed')
    def test_repeated_ng_after_completed_followup_never_replays_it_and_reset_exits_the_same_job(self):
        link,workers,passed,events,control=self.exercise(False)
        self.assertEqual([args['targets'] for method,args in link.calls if method=='command' and args['action']=='play'],[[{'target':1}],[{'target':2}]])
        self.assertEqual(passed,[]);self.assertEqual(events.count('PAUSED'),2)
        self.assertEqual(control.receipts['a'*32]['state'],'failed');self.assertNotIn('RESUMED',events)


class RetryControlDeadlineTests(unittest.TestCase):
    def test_restart_waits_for_a_fresh_inspection_longer_than_old_control_timeout(self):
        import action_protocol
        now=[0.];receipt={'job_id':'a'*32,'request_id':'b'*32,'command':'restart','state':'pending'}
        def state(*args,**kwargs):
            row={**receipt,'state':'completed' if now[0]>=35 else 'pending'}
            return {'job_id':receipt['job_id'],'receipts':{receipt['request_id']:row}}
        with patch.object(action_protocol,'control_state',side_effect=state),patch('time.monotonic',side_effect=lambda:now[0]),patch('time.sleep',side_effect=lambda n:now.__setitem__(0,now[0]+n)):
            self.assertTrue(action_protocol.wait_control('/unused','restart',receipt))
        self.assertGreaterEqual(now[0],35)
