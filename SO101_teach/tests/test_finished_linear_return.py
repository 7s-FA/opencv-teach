"""Only current full-product seating PASS may start the chained linear return."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
import numpy as np
from tests.test_episode_cli import cli,FakeLink,FakeLinear


class FinishedReturnTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=10.;clock=patch.object(cli.time,'monotonic',lambda:self.now);clock.start();self.addCleanup(clock.stop)
        root=patch.object(cli,'ROOT',Path(self.temp.name));root.start();self.addCleanup(root.stop)
        self.link=FakeLink();self.link.mode='HOLD';self.linear=FakeLinear();self.linear.target=1.5
        self.events=[];self.run=cli.Runner(self.link,self.events.append,cli.Control(),30,linear=self.linear)
        self.run.inspection_product='A';self.run.inspection_episode='episode';self.run.inspection_records=[]
        self.run.inspection_log=Path(self.temp.name)/'checks.json';self.run.finished_return_step='placed'
        self.workers={}
        self.pick={'id':'picked','name':'집은 후','inspection':{'station':'linear','target':'1','expected':'empty','timeout_seconds':10}}
        self.place={'id':'placed','name':'완제품 안착','inspection':{'station':'finished','target':'pallet','expected':'cap_added','timeout_seconds':10}}
        def worker(*args,**kwargs):
            check=args[4];value=SimpleNamespace(result=None,close=Mock(),submit=Mock())
            self.workers[check['station']]=value;return value
        for p in (patch('so101_teach.configuration.JigCatalog'),patch('so101_teach.workcell_preview.load_placement',return_value=None),
                  patch('so101_teach.episode_inspection_runtime.FrameInspection',side_effect=worker)):
            p.start();self.addCleanup(p.stop)
        self.run.begin_inspections([self.pick,self.place],{'profile':{'robot_id':'arm3'}},{'PLACE':'placed'})
        self.addCleanup(lambda:self.run.inspection_batch.close() if self.run.inspection_batch else None)

    def observe(self,step,*,state=None,quality='normal',count=2):
        if step['inspection']['station'] not in self.workers:self.run.inspection_batch.start(step)
        item=next(item for item in self.run.inspection_batch.pending if item['step']['id']==step['id'])
        # Capture at the checkpoint before simulating classifier completion.
        # Later motion/pause must consume these exact frames, never invented results.
        while item['frozen_at'] is None:
            self.now+=.05
            self.run.inspection_batch.poll((np.zeros((2,2,3),dtype=np.uint8),self.now))
        for _ in range(count):
            self.now+=.1
            self.workers[step['inspection']['station']].result={'at':item['inflight'],'completed':self.now,'row':{
                'quality':quality,'state':state or step['inspection']['expected'],
                'product':'A','product_certain':True,'seating':'missing' if step==self.pick else 'inside'}}
            self.run.poll_inspections()

    def return_commands(self):
        return [args['target_mm'] for op,args in self.linear.calls if op=='ensure']

    def test_seating_pass_starts_once_while_arm_is_still_returning(self):
        self.observe(self.pick);self.assertEqual(self.return_commands(),[])
        self.link.mode='MOVING';self.link.pending=True;self.linear.phase='MOVING'
        self.observe(self.place,count=1);self.assertEqual(self.return_commands(),[])
        self.observe(self.place,count=1)
        self.assertEqual(self.return_commands(),[100.]);self.assertTrue(self.run.linear_active)
        self.assertLess(self.events.index('INSPECTION_PASS:placed'),next(i for i,e in enumerate(self.events) if e.startswith('CHAIN_RETURN_STARTED')))
        self.run.poll_inspections();self.assertEqual(self.return_commands(),[100.]);self.assertNotIn('DONE',self.events)

    def test_slow_linear_empty_check_finishes_before_return_changes_its_target(self):
        self.observe(self.place);self.assertEqual(self.return_commands(),[])
        self.observe(self.pick);self.assertEqual(self.return_commands(),[100.])

    def test_failed_seating_never_returns(self):
        self.observe(self.pick)
        with self.assertRaisesRegex(ValueError,'안착 불합격'):self.observe(self.place,state='wrong_part',quality='abnormal')
        self.assertEqual(self.return_commands(),[])

    def test_old_frame_and_one_fresh_frame_cannot_return(self):
        self.observe(self.pick);self.run.inspection_batch.start(self.place)
        self.workers['finished'].result={'at':1.,'row':{'quality':'normal','state':'cap_added','product':'A','product_certain':True}}
        self.run.poll_inspections();self.observe(self.place,count=1);self.assertEqual(self.return_commands(),[])

    def test_independent_load_pass_does_not_move_linear(self):
        self.run.finished_return_step=None;self.observe(self.pick);self.observe(self.place)
        self.assertEqual(self.return_commands(),[])

    def test_reset_requested_at_pass_does_not_issue_return(self):
        self.observe(self.pick);self.observe(self.place,count=1)
        self.run.control.pause.set();self.run.control.reset.set()
        self.observe(self.place,count=1)
        self.assertEqual(self.return_commands(),[])

    def test_pause_at_pass_defers_linear_command_until_restart(self):
        self.observe(self.pick);self.observe(self.place,count=1)
        self.run.control.pause.set()
        def resume(seconds):
            self.now+=seconds
            self.assertEqual(self.return_commands(),[])
            if self.run.control.paused:self.run.control.command('restart')
        self.observe(self.place,count=1);self.assertEqual(self.return_commands(),[])
        with patch.object(cli.time,'sleep',side_effect=resume):self.run.check()
        self.run.poll_inspections()
        self.assertEqual(self.return_commands(),[100.])
        self.assertLess(self.events.index('RESUMED'),next(i for i,e in enumerate(self.events) if e.startswith('LINEAR_STARTED') or e.startswith('LINEAR_READY')))

    def test_estop_at_seating_pass_keeps_motion_resume_offset(self):
        self.link.auto_finish=False
        original_poll=self.run.poll_inspections;sent=False
        def poll():
            nonlocal sent
            if not sent:
                sent=True;self.run.inspection_passed.update(('picked','placed'))
                self.link.index=1
                self.run.control.command('estop')
            original_poll()
        def resume(seconds):
            self.now+=seconds
            if self.run.control.paused:
                self.assertEqual(self.return_commands(),[])
                self.link.auto_finish=True;self.run.control.command('restart')
        with patch.object(self.run,'poll_inspections',side_effect=poll),patch.object(cli.time,'sleep',side_effect=resume):
            self.run.move('play',[{'n':1},{'n':2},{'n':3}])
        plays=[args['targets'] for method,args in self.link.calls if method=='command' and args['action']=='play']
        self.assertEqual(plays,[[{'n':1},{'n':2},{'n':3}],[{'n':2},{'n':3}]])
        self.assertEqual(self.return_commands(),[100.])

    def test_only_last_full_product_pallet_check_qualifies(self):
        empty={**self.place,'inspection':{**self.place['inspection'],'expected':'empty'}}
        occupied={**self.place,'inspection':{**self.place['inspection'],'expected':'occupied'}}
        for check in (self.pick,empty,occupied):
            with self.assertRaisesRegex(ValueError,'안착 검사'):cli.finished_return_step({'steps':[check]})
        self.assertEqual(cli.finished_return_step({'steps':[self.place,{**self.place,'id':'last'}]}),'last')

    def test_episode_completion_waits_for_linear_and_does_not_succeed_on_timeout(self):
        for fail in (False,True):
            with self.subTest(timeout=fail):
                link=FakeLink();linear=FakeLinear();events=[]
                run=cli.Runner(link,events.append,cli.Control(),30,linear=linear);run.finished_return_step='placed'
                ep={'steps':[{'id':'safe','name':'safe','ticks':{},'safe_boundary':'end'}]}
                def move(*args,**kwargs):
                    run.linear_return_started=True;run.linear_active=True;run.linear_target=100.;run.linear_until=self.now+12
                    linear.phase='MOVING';linear.target=100.
                def sleep(seconds):
                    self.now+=seconds
                    if not fail:linear.phase='TIMED_COMPLETE'
                with patch.object(run,'move',side_effect=move),patch.object(cli.time,'sleep',side_effect=sleep):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError,'리니어 완료 대기'):run.execute(ep,{},400,1.5)
                        self.assertNotIn('DONE',events)
                    else:
                        run.execute(ep,{},400,1.5)
                        self.assertLess(events.index('CHAIN_RETURN_DONE:target_mm=100:position_measured=false'),events.index('DONE'))
