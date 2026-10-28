"""Real batch + virtual advancing motors: overlap, HOLD, recheck, exact offsets."""
import base64
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
import cv2
import numpy as np
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach.episode_inspection_runtime import InspectionBatch


class MovingLink(FakeLink):
    def __init__(self,now):
        super().__init__();self.now=now;self.mode='HOLD';self.auto_finish=False;self.sent=[];self.started=now[0];self.length=0
    def rpc(self,method,args=None):
        result=super().rpc(method,args)
        if method=='command' and args['action']=='play':
            self.started=self.now[0];self.length=len(args['targets']);self.sent.append((self.now[0],deepcopy(args['targets'])))
        return result
    def state(self):
        if self.error:raise RuntimeError(self.error)
        if self.mode=='MOVING':
            self.index=min(self.length,int((self.now[0]-self.started+1e-7)/.2))
            if self.index==self.length:self.mode='HOLD';self.pending=False;self.completed=self.request
        return super().state()


class DelayedWorker:
    def __init__(self,now,passed,delay):
        self.now=now;self.good=passed;self.delay=delay;self.ready=None;self.value=None;self.captures=[];self.close=Mock()
    @property
    def result(self):return self.value if self.ready is not None and self.now[0]>=self.ready else None
    def submit(self,frame,at):
        self.captures.append(at);self.ready=self.now[0]+self.delay
        self.value={'at':at,'completed':self.ready,'row':{'quality':'normal' if self.good else 'abnormal',
            'state':'housing_seated' if self.good else 'empty','product':'A','product_certain':True}}
        return True


class OverlapResumeTests(unittest.TestCase):
    def setUp(self):
        self.now=[100.];self.clock=patch('time.monotonic',side_effect=lambda:self.now[0]);self.clock.start();self.addCleanup(self.clock.stop)
        self.sleep=patch('time.sleep',side_effect=self.tick);self.sleep.start();self.addCleanup(self.sleep.stop)
        self.on_tick=lambda:None;self.on_pause=lambda:self.fail('Unexpected pause: '+str(self.events[-3:]))
        self.link=MovingLink(self.now);self.control=cli.Control();self.control.save_receipts=Mock();self.events=[];self.workers=[];self.passed=[]
        self.runner=cli.Runner(self.link,self.emit,self.control,30,linear=FakeLinear());self.runner.save_failure_images=Mock()
        self.runner.execution_bundle={'profile':{}};self.runner.inspection_jigs={};self.runner.linear_target=100
        self.steps=[{'id':'check','name':'검사 위치','inspection':{'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5}},
            *[{'id':str(i),'name':'후속 '+str(i)} for i in range(1,4)]]
        self.targets=[{'target':i} for i in range(4)];self.runner.execution_steps=deepcopy(self.steps)
        self.runner.inspection_after_at=self.runner.inspection_after_preview=None
        self.good=lambda attempt:True;self.delay=.12
        def make(step):
            worker=DelayedWorker(self.now,self.good(len(self.workers)),self.delay);self.workers.append(worker);return worker
        self.runner.inspection_batch=InspectionBatch('A',make,lambda step:self.passed.append((step['id'],self.now[0])))
        self.addCleanup(self.runner.inspection_batch.close)
        raw=base64.b64encode(cv2.imencode('.jpg',np.zeros((8,12,3),np.uint8))[1]).decode();original=self.link.post
        def post(endpoint,body=None):
            if endpoint=='/camera' and not self.link.preparing:return {'preview_at':self.now[0],'preview_image':raw}
            return original(endpoint,body)
        self.link.post=post
    def tick(self,seconds):self.now[0]+=seconds;self.on_tick()
    def emit(self,event):
        self.events.append((self.now[0],event))
        if event=='PAUSED':self.on_pause()
    def restart(self,key='a'):
        return self.control.request({'job_id':self.control.job_id,'request_id':key*32,'command':'restart','stop_generation':self.control.stop_generation})
    def run_move(self):return self.runner.move('play',self.targets,self.steps,{})
    def test_normal_pass_overlaps_followup_motion_and_uses_only_original_frames(self):
        self.delay=.4;self.run_move()
        self.assertEqual([targets for _,targets in self.link.sent],[[self.targets[0]],self.targets[1:]])
        started=self.link.sent[1][0];passed_at=self.passed[0][1]
        self.assertLess(started,passed_at);self.assertLessEqual(max(self.workers[0].captures),started)
        self.assertLess(started-self.link.sent[0][0],.4)
        self.assertFalse(any(event=='PAUSED' for _,event in self.events))
        self.assertFalse(self.runner.inspection_batch.pending);self.assertGreaterEqual(self.now[0],passed_at)
    def test_ng_during_motion_holds_and_resumes_only_unfinished_targets_after_fresh_pass(self):
        self.good=lambda attempt:attempt>0
        def paused():
            self.assertEqual(self.link.mode,'HOLD');self.assertEqual(self.link.index,1)
            self.assertEqual(len(self.link.sent),2);self.assertFalse(self.passed);self.restart()
        self.on_pause=paused;self.run_move()
        self.assertEqual([targets for _,targets in self.link.sent],[[self.targets[0]],self.targets[1:],self.targets[2:]])
        self.assertGreater(min(self.workers[1].captures),max(self.workers[0].captures))
        resumed=next(at for at,event in self.events if event=='RESUMED')
        self.assertGreaterEqual(self.link.sent[2][0],resumed);self.assertLessEqual(self.passed[0][1],resumed)
        self.assertEqual(self.control.receipts['a'*32]['state'],'completed')
        completed=[event.split(':',2)[1] for _,event in self.events if event.startswith('STEP_DONE:')]
        self.assertEqual(completed,['1/4','2/4','3/4','4/4'])
    def test_repeated_ng_stays_held_and_reset_never_dispatches_unfinished_targets(self):
        self.good=lambda attempt:False;pauses=[0]
        def paused():
            pauses[0]+=1
            if pauses[0]==1:self.restart()
            else:self.control.command('reset')
        self.on_pause=paused
        with self.assertRaises(cli.ResetRequested):self.run_move()
        self.assertEqual(len(self.link.sent),2);self.assertEqual(self.link.mode,'HOLD')
        self.assertFalse(self.passed);self.assertNotIn('RESUMED',[e for _,e in self.events])
        self.assertEqual(self.control.receipts['a'*32]['state'],'failed')
    def test_late_ng_after_final_motion_rechecks_without_replaying_completed_steps(self):
        self.good=lambda attempt:attempt>0;self.delay=.4
        def paused():self.assertEqual(self.link.index,3);self.restart()
        self.on_pause=paused;self.run_move()
        self.assertEqual(len(self.link.sent),2);self.assertEqual(len(self.passed),1)
        self.assertEqual(self.control.receipts['a'*32]['state'],'completed')
    def test_stop_while_processing_then_ng_cancels_old_restart_and_needs_new_request(self):
        self.good=lambda attempt:attempt>0;stopped=[False];pauses=[0]
        def tick():
            if not stopped[0] and self.now[0]>=100.44:
                stopped[0]=True;self.control.command('estop')
        self.on_tick=tick
        def paused():
            pauses[0]+=1
            if pauses[0]==1:self.restart('a')
            else:
                self.assertEqual(self.control.receipts['a'*32]['state'],'cancelled');self.restart('b')
        self.on_pause=paused;self.run_move()
        self.assertEqual(pauses[0],2);self.assertEqual(self.control.receipts['b'*32]['state'],'completed')
        self.assertEqual([targets for _,targets in self.link.sent],[[self.targets[0]],self.targets[1:],self.targets[1:]])
    def test_transport_loss_remains_fatal_and_outer_plan_holds_before_cleanup(self):
        lost=[False]
        def tick():
            if not lost[0] and self.now[0]>=100.44:
                lost[0]=True;self.runner.linear.error='linear disconnected'
        self.on_tick=tick
        self.runner.inspection_records=[];self.runner.inspection_episode='fixture';self.runner.inspection_product='A'
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            self.runner.inspection_log=Path(folder)/'checks.json'
            with patch.object(self.runner,'begin_inspections'):
                with self.assertRaisesRegex(RuntimeError,'linear disconnected'):
                    self.runner.play_plan({'completion_events':{}},self.steps,{},[{'ticks':target} for target in self.targets])
        self.assertEqual(self.link.mode,'HOLD');self.assertEqual(len(self.link.sent),2)
        self.assertNotIn('RESUMED',[event for _,event in self.events])

    def test_failure_at_different_followup_offsets_never_replays_completed_steps(self):
        # Include result arrival before the first completion, mid-program and
        # after every target finished, with small scheduler phase offsets.
        for repeat in range(10):
            for delay in (.01,.06,.12,.2,.4,.7):
                with self.subTest(repeat=repeat,delay=delay):
                    f=OverlapResumeTests();f.setUp()
                    try:
                        f.now[0]+=repeat*.001;f.delay=delay;f.good=lambda attempt:attempt>0;paused=[]
                        def pause():
                            paused.append(f.link.index);self.assertEqual(len(paused),1);f.restart()
                        f.on_pause=pause;f.run_move()
                        self.assertEqual(len(paused),1)
                        expected=[[f.targets[0]],f.targets[1:]]
                        if paused[0]<3:expected.append(f.targets[1+paused[0]:])
                        self.assertEqual([targets for _,targets in f.link.sent],expected)
                        self.assertEqual([e.split(':',2)[1] for _,e in f.events if e.startswith('STEP_DONE:')],['1/4','2/4','3/4','4/4'])
                        self.assertEqual(f.control.receipts['a'*32]['state'],'completed')
                    finally:f.doCleanups()
