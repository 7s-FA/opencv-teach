from copy import deepcopy
import json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock,patch
from so101_teach.domain import ROOT,read_json
from so101_teach.episode_inspection import validate_inspection,segments,InspectionDecision
from so101_teach.episode_execution import InspectedExecution
from tests import test_ui as ui_fixture
from tests import test_episode_cli as cli_fixture
from tests import test_episode_transfer as transfer_fixture

CHECK={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5.}
def result(at=10.1,**changes):
    return {'at':at,'row':{'quality':'normal','state':'housing_seated','product':'A','product_certain':True,'reason':'관측 결과',**changes}}

class PolicyTests(unittest.TestCase):
    def test_legacy_episode_has_no_implicit_checks(self):
        doc={'steps':[{'id':'a'}]};before=deepcopy(doc);validate_inspection(doc);self.assertEqual(doc,before)
    def test_bad_product_target_timeout_and_safe_step_are_rejected(self):
        doc={'product_type':'A','steps':[{'id':'a','inspection':deepcopy(CHECK)}]}
        for bad in ({'station':'other'},{'target':[]},{'expected':'present'},{'timeout_seconds':float('nan')},{'timeout_seconds':31}):
            changed=deepcopy(doc);changed['steps'][0]['inspection'].update(bad)
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_inspection(changed)
        doc['product_type']=None
        with self.assertRaises(ValueError):validate_inspection(doc)
        doc['product_type']='A';doc['steps'][0]['safe_boundary']='start'
        with self.assertRaises(ValueError):validate_inspection(doc)
    def test_segments_stop_exactly_at_checkpoint_without_mutating_inputs(self):
        steps=[{'id':'a','inspection':CHECK},{'id':'b'},{'id':'c','inspection':CHECK},{'id':'d'}];targets=[{'q':i} for i in range(4)];before=deepcopy(steps)
        parts=segments(steps,targets);self.assertEqual([len(p['targets']) for p in parts],[1,2,1]);parts[0]['targets'][0]['q']=9;self.assertEqual(targets[0]['q'],0);self.assertEqual(steps,before)
    def test_two_distinct_fresh_normal_frames_are_required(self):
        d=InspectionDecision(CHECK,'A',10);self.assertFalse(d.observe(result(),10.2));self.assertFalse(d.observe(result(),10.3));self.assertTrue(d.observe(result(10.4),10.5))
    def test_stale_prearrival_reordered_and_gapped_frames_never_count_as_pair(self):
        d=InspectionDecision(CHECK,'A',10)
        for at,now in ((9.,10.1),(10.2,10.3),(10.1,10.4),(12.,12.1)):self.assertFalse(d.observe(result(at),now))
        self.assertTrue(d.observe(result(12.2),12.3))
    def test_wrong_product_or_expected_stage_stops_even_if_shape_is_normal(self):
        for change in ({'product':'B'},{'state':'cap_added'},{'quality':'outside'}):
            d=InspectionDecision(CHECK,'A',10);d.observe(result(**change),10.2)
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'불합격'):d.observe(result(10.3,**change),10.4)
    def test_uncertain_and_missing_observations_fail_on_timeout(self):
        for observation in (None,result(10.1,product_certain=False)):
            d=InspectionDecision(CHECK,'A',10);self.assertFalse(d.observe(observation,10.2))
            with self.assertRaisesRegex(ValueError,'시간 초과'):d.observe(observation,15)
    def test_worker_failure_never_becomes_pass(self):
        with self.assertRaisesRegex(ValueError,'검사 오류'):InspectionDecision(CHECK,'A',10).observe({'error':'camera unavailable'},10.1)

class PCExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.clock=10.
        p=patch('so101_teach.episode_execution.time.monotonic',lambda:self.clock);p.start();self.addCleanup(p.stop)
        self.s=SimpleNamespace(running=True,error=None,state='HOLD',completed_request_id=None,active_request_id=None,command_pending=Event(),program_active=Event(),request=Mock())
        self.a=SimpleNamespace(data_dir=Path(self.temp.name),episode={'id':'e','product_type':'A'},session=self.s,latest=SimpleNamespace(fresh=lambda:True,calibration_matches=True),profile={},catalog={},workcell_preview={},camera=SimpleNamespace(error=None),camera_view_observation=lambda:None,start_camera=Mock(),camera_needed=lambda:False,suspend_camera=Mock(),notice=Mock())
        def dispatch(action,targets):self.s.active_request_id=len(self.a.motion_request.call_args_list);self.s.completed_request_id=None;return self.s.active_request_id
        self.a.motion_request=Mock(side_effect=dispatch)
        self.worker=SimpleNamespace(result=None,submit=Mock(),close=Mock())
        p=patch('so101_teach.episode_execution.FrameInspection',return_value=self.worker);p.start();self.addCleanup(p.stop)
        self.steps=[{'id':'gate','name':'검사','inspection':CHECK},{'id':'next','name':'다음'}]
        self.run=InspectedExecution(self.a,self.steps,[{'q':1},{'q':2}]);self.run.dispatch()
    def arrive(self):self.s.completed_request_id=self.run.request_id;self.run.poll()
    def test_selected_step_completes_before_check_and_motion_keeps_running(self):
        self.s.state='MOVING';self.s.index=0;self.run.poll();self.assertFalse(self.run.batch.pending)
        self.s.index=1;self.run.poll();self.assertTrue(self.run.batch.pending)
        self.assertEqual(self.a.motion_request.call_args.args[1],[{'q':1},{'q':2}])
        self.clock=10.2;self.worker.result=result();self.run.poll()
        self.clock=10.5;self.worker.result=result(10.4);self.run.poll();self.assertFalse(self.run.batch.pending)
        self.s.state='HOLD';self.arrive();self.assertEqual(self.run.phase,'done')
    def test_failure_during_motion_sends_hold(self):
        self.s.state='MOVING';self.s.index=2;self.run.poll()
        self.clock=10.2;self.worker.result=result(quality='abnormal');self.run.poll()
        self.clock=10.5;self.worker.result=result(10.4,quality='abnormal');self.run.poll()
        self.assertEqual(self.run.phase,'failed');self.s.request.assert_called_once_with('hold')
    def test_operator_cancel_discards_late_pass_and_never_resumes(self):
        self.arrive();self.run.cancel();self.worker.result=result();self.run.poll();self.a.motion_request.assert_called_once();self.worker.close.assert_called_once()
    def test_lost_hold_or_camera_error_stops(self):
        self.arrive();self.s.state='READ_ONLY';self.run.poll();self.assertEqual(self.run.phase,'failed');self.s.request.assert_called_once_with('hold')
    def test_final_completion_waits_for_check_and_timeout_holds(self):
        self.arrive();self.assertEqual(self.run.phase,'inspection');self.clock=16.;self.run.poll();self.assertEqual(self.run.phase,'failed');self.assertIn('시간 초과',self.run.error)

class PiExecutionTests(unittest.TestCase):
    def test_pi_continuous_path_checks_after_selected_step_and_defers_done(self):
        from so101_teach.episode_inspection_runtime import InspectionBatch
        from so101_teach.episode_inspection import inspection_schedule
        cli=cli_fixture.cli;link=cli_fixture.FakeLink();events=[];runner=cli.Runner(link,events.append,cli.Control(),20,linear=cli_fixture.FakeLinear())
        episode={'id':'e','product_type':'A','steps':[{'id':'a','name':'하단 놓고 들기','ticks':{'q':1},'inspection':CHECK},{'id':'b','name':'회피','ticks':{'q':2}},{'id':'c','name':'계속','ticks':{'q':3}}],'completion_events':{'LOWER':'a'}}
        pending=[]
        def begin(*args):
            runner.inspection_due=inspection_schedule(episode['steps'])
            runner.inspection_batch=SimpleNamespace(pending=pending,start=lambda step:pending.append(step),close=lambda:pending.clear())
        def poll():
            if not pending:return
            self.assertIn('STEP_DONE:1/3:하단 놓고 들기',events);self.assertNotIn('STAGE_DONE:LOWER',events)
            events.extend(['INSPECTION_PASS:a','STAGE_DONE:LOWER']);pending.clear()
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(runner,'begin_inspections',side_effect=begin),patch.object(runner,'poll_inspections',side_effect=poll),patch.object(runner,'release_after_completion'):
            runner.execute(episode,{},400,100.)
        plays=[a for m,a in link.calls if m=='command' and a['action']=='play'];self.assertEqual(len(plays),1);self.assertEqual(len(plays[0]['targets']),3)
        self.assertLess(events.index('INSPECTION_PASS:a'),events.index('EPISODE_DONE'));self.assertEqual(events[-1],'DONE')

class InspectionUITests(unittest.TestCase):
    setUp=ui_fixture.UITests.setUp
    tearDown=ui_fixture.UITests.tearDown
    def configure(self):
        a=self.app;a.step_name.set('집게 회피 후 검사');a.commit_target();a.open_episode_adjust();p=a.episode_adjust_panel.inspection;a.episode_adjust_panel.tabs.select(p);self.root.update();p.product.set('B');p.station.set('운반용 지그');p.location_changed();p.target.set('중단 부품');p.save_check();return p
    def test_product_check_persist_after_rename_and_delete(self):
        p=self.configure();a=self.app;key=a.selected;check=deepcopy(a.episode['steps'][0]['inspection']);a.step_name.set('다른 이름');a.update_selected_step()
        self.assertEqual(a.episode['steps'][0]['inspection'],check);self.assertEqual(a.store.load(a.store.directory/(a.episode['id']+'.json'))['product_type'],'B')
        p.remove_check();self.assertNotIn('inspection',a.episode['steps'][0])
    def test_cannot_clear_product_while_check_exists(self):
        p=self.configure();before=deepcopy(self.app.episode);p.product.set('미지정')
        with self.assertRaises(ValueError):p.save_product()
        self.assertEqual(self.app.episode,before)
    def test_new_settings_and_old_controls_fit_supported_sizes(self):
        from PIL import ImageGrab
        p=self.configure();a=self.app;out=ROOT/'verification/episode-seating-gates'
        for size in ('1920x1080',):
            self.root.geometry(size);self.root.update();self.assertTrue(p.save_button.winfo_ismapped());self.assertLessEqual(p.save_button.winfo_rooty()+p.save_button.winfo_height(),a.pages['teach'].winfo_rooty()+a.pages['teach'].winfo_height())
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/f'settings-{size}.png')

class TransferTests(unittest.TestCase):
    setUp=transfer_fixture.EpisodeTransferTests.setUp
    tearDown=transfer_fixture.EpisodeTransferTests.tearDown
    def test_checked_episode_cannot_reach_old_pi_and_roundtrips_when_supported(self):
        self.request['episode']['product_type']='A';self.request['episode']['steps'][0]['inspection']=deepcopy(CHECK)
        retreat=deepcopy(self.request['episode']['steps'][0]);retreat['id']='retreat';retreat['name']='회피';retreat.pop('inspection');self.request['episode']['steps'].append(retreat)
        (self.root/'integration/episode_cli.py').write_text('# old runner')
        with self.assertRaisesRegex(ValueError,'안착 검사'):transfer_fixture.receiver.deliver(self.root,self.request)
        (self.root/'integration/episode_cli.py').write_text('INSPECTION_PROTOCOL = 6')
        target=self.root/'inspection';target.mkdir()
        for name in ('roi_reference.json','shape_templates.json','shape_templates.npz','appearance_reference.json'):(target/name).write_bytes((ROOT/'inspection'/name).read_bytes())
        from so101_teach.episode_inspection import criteria_signature
        for name in ('episode_inspection.py','episode_inspection_runtime.py','shape_inspection.py','inspection_geometry.py','inspection_seating.py'):
            dest=self.root/'so101_teach'/name;dest.parent.mkdir(exist_ok=True);dest.write_bytes((ROOT/'so101_teach'/name).read_bytes())
        with self.assertRaisesRegex(ValueError,'기준이 다릅니다'):transfer_fixture.receiver.deliver(self.root,self.request)
        self.request['inspection_criteria_sha256']=criteria_signature()
        invalid=deepcopy(self.request);invalid['episode']['steps'].pop()
        before={str(p):p.read_bytes() for p in self.root.rglob('*.json')}
        self.assertTrue(transfer_fixture.receiver.deliver(self.root,invalid,check_only=True)['check_only'])
        self.assertEqual({str(p):p.read_bytes() for p in self.root.rglob('*.json')},before)
        receipt=transfer_fixture.receiver.deliver(self.root,self.request);saved=read_json(self.data/'episodes'/(receipt['episode_id']+'.json'))
        self.assertEqual(saved['product_type'],'A');self.assertEqual(saved['steps'][0]['inspection'],CHECK)

class RecordedVisionTests(unittest.TestCase):
    def observe(self,folder):
        import cv2
        from so101_teach.inspection_geometry import anchors_for
        from so101_teach.shape_inspection import ShapeInspector
        config=read_json(folder/'fixture.json');suffix='png' if folder.name=='insert-normal' else 'jpg';frame=cv2.imread(str(folder/('camera-0.'+suffix)))
        anchors,_=anchors_for(frame,{},config['adopted'],config['catalog'],config['profile'],config['reference'],'운반용 지그','전체',{})
        row=next(r for r in ShapeInspector().inspect(frame,anchors,config['profile']) if r['id']=='b_tpu')
        decision=InspectionDecision({**CHECK,'station':'carrier','target':'insert','expected':'present'},'B',10)
        decision.observe({'at':10.1,'row':row},10.2);return decision.observe({'at':10.3,'row':row},10.4)
    def test_real_normal_insert_passes_existing_visual_criteria(self):self.assertTrue(self.observe(ROOT/'tests/fixtures/insert-normal'))
    def test_real_displaced_insert_stops_existing_visual_criteria(self):
        with self.assertRaisesRegex(ValueError,'불합격'):self.observe(ROOT/'tests/fixtures/b-housing-seating')

class FramePipelineTests(unittest.TestCase):
    def test_new_camera_frame_is_localized_and_inspected_without_saved_pose(self):
        import cv2
        from so101_teach.configuration import JigCatalog
        from so101_teach.episode_inspection_runtime import FrameInspection
        folder=ROOT/'tests/fixtures/insert-normal';cfg=read_json(folder/'fixture.json');frame=cv2.imread(str(folder/'camera-0.png'))
        with tempfile.TemporaryDirectory() as temp:
            catalog=JigCatalog(temp,profile=cfg['profile']);catalog.items=cfg['catalog']
            worker=FrameInspection(temp,cfg['profile'],catalog,{},dict(station='carrier',target='insert',expected='present',timeout_seconds=5),'A')
            try:
                worker.submit(frame,10.1);until=time.monotonic()+8
                while worker.result is None and time.monotonic()<until:time.sleep(.02)
                self.assertIsNotNone(worker.result);self.assertNotIn('error',worker.result);self.assertIsNotNone(worker.result['row'],worker.result)
                self.assertEqual(worker.result['row']['quality'],'normal',worker.result['row']);self.assertEqual(worker.result['row']['product'],'A')
            finally:worker.close()
    def test_stop_command_is_sent_before_inspection_cleanup_can_fail(self):
        f=ui_fixture.UITests();f.setUp()
        try:
            from so101_teach.motion import MotionSession
            a=f.app;s=MotionSession('fake',a.calibration);s.running=True;s.request=Mock(return_value=7);a.session=s
            a.inspection_run=SimpleNamespace(busy=True,dispatching=False,cancel=Mock(side_effect=ValueError('cleanup')))
            a.motion_request('hold');s.request.assert_called_once_with('hold',None)
            a.inspection_run=None;a.session=None
        finally:f.app.inspection_run=None;f.app.session=None;f.tearDown()

class AppIntegrationTests(unittest.TestCase):
    setUp=ui_fixture.UITests.setUp
    tearDown=ui_fixture.UITests.tearDown
    def test_real_app_dispatches_only_first_checkpoint_and_rejects_overlapping_move(self):
        from so101_teach.motion import MotionSession
        a=self.app;a.step_name.set('검사');a.commit_target();a.episode['product_type']='A';a.episode['steps'][0]['inspection']=deepcopy(CHECK)
        a.step_name.set('다음');a.target['shoulder_pan']+=3;a.commit_target()
        s=MotionSession('fake',a.calibration);s.running=True;s.state='HOLD';s.request=Mock(return_value=7);a.session=s
        try:
            a.begin_execution('play',deepcopy(a.episode['steps']),{})
            self.assertEqual(s.request.call_args.args,('play',[step['ticks'] for step in a.episode['steps']]))
            with self.assertRaisesRegex(ValueError,'실행 중'):a.motion_request('move',[a.target])
            a.motion_request('hold');self.assertEqual(a.inspection_run.phase,'failed');self.assertEqual(s.request.call_args.args,('hold',None))
        finally:a.inspection_run=None;a.session=None
    def test_unsaved_inspection_draft_blocks_export_without_writes(self):
        p=self.app.episode_adjust_panel.inspection;p.product.set('A');self.assertTrue(self.app.episode_adjust_panel.dirty)
        export=self.app.settings.episode_transfer_panel;export.target={'arm':'arm2'}
        with patch.object(self.app.settings.pool,'submit') as submit:
            with self.assertRaisesRegex(ValueError,'저장한 뒤'):export.send()
            submit.assert_not_called()
    def test_integration_never_reports_done_while_inspecting_or_after_failure(self):
        from tests import test_integration as fixtures
        fixture=fixtures.IntegrationTests();fixture.setUp()
        try:
            app=fixture.start();app.inspection_run=SimpleNamespace(phase='inspection',busy=True,error=None)
            fixture.complete();self.assertIsNotNone(fixture.c.active);self.assertFalse(any(text.endswith('_DONE') for _,text in fixture.messages))
            app.inspection_run.phase='failed';app.inspection_run.busy=False;app.inspection_run.error='안착 불합격'
            fixture.c.poll();fixture.c.poll();self.assertIn('INSPECTION_FAILED',fixture.messages[-1][1]);self.assertNotIn('_DONE',fixture.messages[-1][1])
        finally:fixture.tearDown()
