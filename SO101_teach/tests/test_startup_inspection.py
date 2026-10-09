from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock,patch
import tempfile,time,unittest
from tests import test_ui as ui_fixture,test_episode_cli as cli_fixture
from so101_teach.episode_inspection import validate_inspection,has_inspections,startup_steps,InspectionDecision
from so101_teach.episode_execution import StartupInspectionRun

EMPTY={'station':'linear','target':'1','expected':'empty','timeout_seconds':5}
ALL={'station':'carrier','target':'all','expected':'present','timeout_seconds':5}

class PolicyTests(unittest.TestCase):
    def test_episode_start_checks_expand_without_moving_or_modifying_steps(self):
        episode={'id':'e','product_type':'B','startup_inspections':[ALL,EMPTY],'steps':[{'id':'s','ticks':{}}]};before=deepcopy(episode)
        validate_inspection(episode);self.assertTrue(has_inspections(episode));checks=startup_steps(episode)
        self.assertEqual(len(checks),4);self.assertEqual([s['inspection_product'] for s in checks],['B','B','B','A']);self.assertEqual(episode,before);self.assertFalse(any('ticks' in s for s in checks))
    def test_all_products_and_invalid_start_conditions(self):
        episode={'product_type':'A','startup_inspections':[{**ALL,'target':'all_products'}]};self.assertEqual(len(startup_steps(episode)),6)
        for value in ('invalid',[{**ALL,'expected':'empty'}],[ALL]*13):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_inspection({**episode,'startup_inspections':value})
        with self.assertRaises(ValueError):validate_inspection({'startup_inspections':[EMPTY]})
    def test_empty_requires_two_fresh_confirmed_empty_frames_without_product_certainty(self):
        decision=InspectionDecision(EMPTY,'B',10);row={'state':'empty','quality':'abnormal','seating':'missing','product':'A','product_certain':False}
        self.assertFalse(decision.observe({'at':10.1,'row':row},10.2));self.assertTrue(decision.observe({'at':10.3,'row':row},10.4))
    def test_empty_never_accepts_unknown_obscured_or_boundary_observations(self):
        for row in ({'state':'unknown','quality':'waiting'},{'state':'empty','quality':'waiting'},{'state':'empty','quality':'outside'},{'state':'empty','quality':'abnormal','seating':'boundary'}):
            decision=InspectionDecision(EMPTY,'A',10)
            decision.observe({'at':10.1,'row':row},10.2)
            try:passed=decision.observe({'at':10.3,'row':row},10.4)
            except ValueError:continue
            self.assertFalse(passed)
            with self.assertRaisesRegex(ValueError,'시간 초과'):decision.observe(None,15)
    def test_presence_and_seating_are_separate_conditions(self):
        row={'state':'housing_seated','quality':'abnormal','seating':'boundary','product':'B','product_certain':True}
        exists=InspectionDecision({**EMPTY,'target':'2','expected':'occupied'},'A',10)
        exists.observe({'at':10.1,'row':row},10.2);self.assertTrue(exists.observe({'at':10.3,'row':row},10.4))
        seated=InspectionDecision({**EMPTY,'target':'2','expected':'housing_seated'},'A',10)
        seated.observe({'at':10.1,'row':row},10.2)
        with self.assertRaisesRegex(ValueError,'불합격'):seated.observe({'at':10.3,'row':row},10.4)
    def test_right_pallet_expects_b_even_when_episode_product_is_a(self):
        row={'state':'housing_seated','quality':'normal','product':'B','product_certain':True}
        decision=InspectionDecision({**EMPTY,'target':'2','expected':'housing_seated'},'A',10)
        decision.observe({'at':10.1,'row':row},10.2);self.assertTrue(decision.observe({'at':10.3,'row':row},10.4))

class PCStartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.now=10.
        patcher=patch('so101_teach.episode_execution.time.monotonic',lambda:self.now);patcher.start();self.addCleanup(patcher.stop)
        s=self.session=SimpleNamespace(running=True,error=None,state='HOLD',command_pending=Event(),program_active=Event(),request=Mock())
        self.app=SimpleNamespace(data_dir=Path(self.temp.name),episode={'id':'e','product_type':'A','startup_inspections':[EMPTY],'steps':[]},session=s,latest=SimpleNamespace(fresh=lambda:True,calibration_matches=True),profile={},catalog={},workcell_preview={},start_camera=Mock(),camera_needed=lambda:False,suspend_camera=Mock(),notice=Mock(),camera=None)
        self.workers=[]
        def worker(*args,**kwargs):
            value=SimpleNamespace(result=None,submit=Mock(),close=Mock(),retain=Mock());self.workers.append(value);return value
        patcher=patch('so101_teach.episode_execution.FrameInspections',side_effect=worker);patcher.start();self.addCleanup(patcher.stop)
        self.continue_run=Mock()
    def run_start(self):
        self.run=StartupInspectionRun(self.app,self.continue_run);self.app.inspection_run=self.run;self.run.start()
    def pair(self,**row):
        for at in (self.now+.1,self.now+.3):
            self.now=at+.01;key=next(iter(self.run.decision.pending),self.run.checks[0]['id']);self.workers[-1].result={'results':{key:{'at':at,'row':row}}};self.run.poll()
    def test_no_movement_before_all_initial_conditions_pass(self):
        self.app.episode['startup_inspections']=[EMPTY,{**EMPTY,'target':'2'}];self.run_start()
        self.pair(state='empty',quality='abnormal',seating='missing');self.continue_run.assert_not_called();self.session.request.assert_not_called()
        self.pair(state='empty',quality='abnormal',seating='missing');self.continue_run.assert_called_once();self.assertEqual(self.run.phase,'done')
    def test_initial_failure_holds_and_blocks_all_movement(self):
        self.run_start();self.pair(state='housing_seated',quality='normal',product='A',product_certain=True)
        self.continue_run.assert_not_called();self.session.request.assert_called_once_with('hold');self.assertEqual(self.run.phase,'failed')
    def test_initial_cancel_and_timeout_never_resume(self):
        self.run_start();self.run.cancel(hold=True);self.pair(state='empty',quality='abnormal');self.continue_run.assert_not_called()
        self.run_start();self.now=20.;self.run.poll();self.assertEqual(self.run.phase,'failed');self.continue_run.assert_not_called()
    def test_missing_hold_blocks_camera_worker_and_continuation(self):
        self.session.state='MOVING';self.run_start();self.assertEqual(self.workers,[]);self.continue_run.assert_not_called();self.assertEqual(self.run.phase,'failed')

class PiStartTests(unittest.TestCase):
    def episode(self):return {'id':'e','product_type':'A','startup_inspections':[ALL,EMPTY],'steps':[{'id':'s','name':'안전','ticks':{},'safe_boundary':'start'},{'id':'n','name':'작업','ticks':{}}]}
    def test_start_checks_precede_linear_and_safe_pose_and_only_then_move(self):
        cli=cli_fixture.cli;link=cli_fixture.FakeLink();linear=cli_fixture.FakeLinear();runner=cli.Runner(link,lambda x:None,cli.Control(),20,linear=linear);seen=[]
        def inspect(steps,bundle):
            self.assertEqual(len(steps),4);self.assertFalse(any(m=='command' for m,_ in link.calls));self.assertTrue(any(op=='ensure' for op,_ in linear.calls));seen.extend(step['id'] for step in steps)
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(runner,'inspect_startup_group',side_effect=inspect),patch.object(runner,'measure',return_value={}),patch.object(runner,'release_after_completion'):
            runner.execute(self.episode(),{},400,100.)
        self.assertEqual(len(seen),4);self.assertTrue(any(op=='ensure' for op,_ in linear.calls));self.assertTrue(any(m=='command' and args['action']=='move' for m,args in link.calls))
    def test_start_failure_prevents_linear_and_motor_commands(self):
        cli=cli_fixture.cli;link=cli_fixture.FakeLink();linear=cli_fixture.FakeLinear();runner=cli.Runner(link,lambda x:None,cli.Control(),20,linear=linear)
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(runner,'inspect_startup_group',side_effect=ValueError('부품 없음')),patch.object(runner,'measure',return_value={}):
            with self.assertRaisesRegex(ValueError,'부품 없음'):runner.execute(self.episode(),{},400,100.)
        self.assertFalse(any(m=='command' for m,_ in link.calls));self.assertTrue(any(op=='ensure' for op,_ in linear.calls))

    def inspect(self,phase='TIMED_COMPLETE'):
        cli=cli_fixture.cli;clock=[100.];link=cli_fixture.FakeLink();linear=cli_fixture.FakeLinear();linear.phase=phase;events=[]
        runner=cli.Runner(link,events.append,cli.Control(),20,linear=linear);runner.inspection_product='A';runner.inspection_episode='e';runner.inspection_records=[]
        class Worker:
            @property
            def result(self):return {'at':clock[0]-.01,'row':{'state':'empty','quality':'abnormal','seating':'missing','product_certain':False}}
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),patch('so101_teach.configuration.JigCatalog'),patch('so101_teach.workcell_preview.load_placement',return_value={'linear_stage':{}}),patch('so101_teach.episode_inspection_runtime.FrameInspection',return_value=Worker()):
            runner.deadline=120;runner.inspection_log=Path(folder)/'inspection.json'
            if phase=='MOVING':
                with self.assertRaisesRegex(RuntimeError,'정지'):runner.inspect_checkpoint({'id':'start','name':'시작 조건','inspection':EMPTY},{'profile':{}},startup=True)
            else:runner.inspect_checkpoint({'id':'start','name':'시작 조건','inspection':EMPTY},{'profile':{}},startup=True)
        return runner,link,linear,events
    def test_actual_pi_start_inspection_accepts_torque_off_without_arming(self):
        runner,link,linear,events=self.inspect();self.assertIn('INSPECTION_PASS:start',events);self.assertEqual(runner.inspection_records[0]['result'],'PASS')
        self.assertFalse(any(m=='command' for m,_ in link.calls));self.assertTrue(all(op=='status' for op,_ in linear.calls))
    def test_moving_linear_is_logged_as_failed_start_without_retargeting(self):
        runner,link,linear,events=self.inspect('MOVING');self.assertEqual(runner.inspection_records[0]['result'],'FAIL');self.assertTrue(any(e.startswith('INSPECTION_FAILED:') for e in events));self.assertTrue(all(op=='status' for op,_ in linear.calls))

class UITests(unittest.TestCase):
    setUp=ui_fixture.UITests.setUp
    tearDown=ui_fixture.UITests.tearDown
    def test_product_is_common_and_start_checks_save_without_selecting_step(self):
        a=self.app;p=a.episode_adjust_panel;self.root.geometry('1920x1080');a.open_episode_adjust();p.inspection.product.set('A');p.inspection.save_product();p.tabs.select(p.startup);self.root.update()
        self.assertTrue(p.common.winfo_ismapped());self.assertIsNone(a.selected);p.startup.save()
        self.assertEqual(a.episode['startup_inspections'],[ALL]);self.assertEqual(a.episode['product_type'],'A')
        p.startup.station.set('리니어 조립');p.startup.location_changed();p.startup.save();self.assertEqual(a.episode['startup_inspections'][1],EMPTY)
        p.startup.table.selection_set('0');p.startup.select();p.startup.remove();self.assertEqual(a.episode['startup_inspections'],[EMPTY]);self.assertFalse(p.dirty)
    def test_start_drafts_block_export_and_survive_episode_switch(self):
        a=self.app;a.commit_target();eid=a.episode['id'];p=a.episode_adjust_panel;p.inspection.product.set('B');p.inspection.save_product();p.startup.timeout.set('7');self.assertTrue(p.dirty)
        a.new_episode();self.assertFalse(p.dirty);index=next(i for i,(_,d) in enumerate(a.library_entries) if d['id']==eid);a.library.selection_clear(0,'end');a.library.selection_set(index);a.load_selected_episode()
        self.assertEqual(p.startup.timeout.get(),'7');self.assertTrue(p.dirty);p.startup.discard();self.assertFalse(p.dirty)
    def test_startup_blocks_safe_pose_in_actual_app_until_pass(self):
        from so101_teach.motion import MotionSession
        a=self.app;a.commit_target();a.add_safe_steps();a.episode['product_type']='A';a.episode['startup_inspections']=[EMPTY]
        with patch('so101_teach.episode_execution.StartupInspectionRun') as run,patch.object(a,'motion_request') as motion:
            a.execute_episode();motion.assert_not_called();run.return_value.start.assert_called_once()
            run.call_args.args[1]();motion.assert_called_once_with('move',[a.episode['steps'][0]['ticks']])
        a.inspection_run=None
