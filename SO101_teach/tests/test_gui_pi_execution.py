import io
import json
import queue
import signal
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from tests import test_arm_workspaces as workspaces
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach import pi_execution as gui
from so101_teach import pi_execution_receiver as receiver
from so101_teach.domain import ROOT,JOINTS,atomic_json
from so101_teach.remote_client import RemoteLink,RemoteCameraSession


class GuiExecutionTests(unittest.TestCase):
    setUp=workspaces.ArmWorkspacesTests.setUp
    tearDown=workspaces.ArmWorkspacesTests.tearDown

    def ready(self):
        for key,app in self.manager.apps.items():
            session=workspaces.ArmWorkspacesTests.connected(self,app)
            for value in session.latest.telemetry.values():value['moving']=0
            app.remote_mode=True
            app.remote=SimpleNamespace(config={'host':'example.test','port':22,'user':'robot','app_dir':'/robot','python':'python3'},
                error=None,stop=threading.Event(),lease='lease-'+key,server_port=8765 if key=='arm2' else 8766,
                token='test-token',instance=key,close=Mock(),rpc=Mock(return_value={}),bundle_hash=None)
        return self.a

    def test_diagnostics_switch_optional_tab_does_not_hide_both_windows(self):
        self.a.settings.open_diagnostics();self.root.update()
        self.a.show_page('devices');self.a.dual_monitor.open_diagnostics('arm3');self.root.update()
        self.assertEqual(self.manager.active,'arm3');self.assertNotEqual(self.b.root.state(),'withdrawn')
        self.assertEqual(self.b.settings.tabs.tab('current','text'),'진단 기록')
        self.b.dual_monitor.open_diagnostics('arm2');self.root.update()
        self.assertNotEqual(self.a.root.state(),'withdrawn');self.assertEqual(self.a.page,'settings')

    def test_request_contains_selected_snapshot_without_recipe_rebinding(self):
        app=self.ready();app.episode=app.store.new('선택한 새 에피소드');app.episode['product_type']='B'
        app.episode['steps']=[app.store.step(app.target,'선택 스텝')];app.episode_name.set(app.episode['name'])
        request=gui.execution_request(app,self.manager.apps)
        self.assertEqual(request['episode']['id'],app.episode['id']);self.assertEqual(request['arm'],'arm2')
        self.assertEqual(set(request['links']),{'arm2','arm3'});self.assertNotIn('slot',request)
        request['episode']['name']='changed';self.assertNotEqual(app.episode['name'],'changed')
        self.a.session.request.assert_not_called();self.b.session.request.assert_not_called()

    def test_other_arm_motion_blocks_full_episode_before_launch(self):
        app=self.ready();self.b.session.state='MOVING'
        with self.assertRaisesRegex(ValueError,'정지 상태'):gui.execution_request(app,self.manager.apps)
        self.a.session.request.assert_not_called()

    def test_gui_buttons_use_full_pi_runner_and_keep_raw_mode_explicit(self):
        self.a.remote_mode=True
        with patch.object(gui,'PiExecution') as launch,patch.object(self.a,'prepare_execution') as old:
            self.a.execute_episode();launch.assert_called_once_with(self.a);old.assert_not_called()
            launch.reset_mock();self.a.execute_taught_episode();launch.assert_called_once_with(self.a,apply_jig=False)

    def test_full_gui_job_records_pi_events_and_releases_ui_ownership(self):
        app=self.ready();process=SimpleNamespace(stdin=io.StringIO(),stdout=io.StringIO('B_LINEAR_STARTED:target_mm=100\nB_STAGE_DONE:LOWER\nB_DONE\n'),wait=Mock(return_value=0),poll=Mock(return_value=0))
        with patch.object(gui.subprocess,'Popen',return_value=process):
            job=gui.PiExecution(app)
            if job.thread:job.thread.join(2)
            job.poll()
        self.assertFalse(job.busy);self.assertIsNone(self.a.remote.execution_owner);self.assertIsNone(self.b.remote.execution_owner)
        record=json.loads(job.path.read_text());self.assertEqual(record['state'],'done')
        self.assertIn('B_STAGE_DONE:LOWER',[e['status'] for e in record['events']])
        self.assertNotIn('test-token',job.path.read_text());self.assertIn('에피소드 완료',app.message.get())
        self.a.session.request.assert_not_called();self.b.session.request.assert_not_called()

    def test_observer_without_accepted_pose_keeps_render_results_as_mapping(self):
        self.a.camera=SimpleNamespace(observer=True,observation=None,error=None,recovering=False,running=False,close=Mock())
        self.assertEqual(self.a.camera_adoption_results(),{})
        self.assertEqual(self.a.teaching_pause_results(),{})

    def test_stop_is_forwarded_to_job_before_manual_motion(self):
        self.ready();job=SimpleNamespace(busy=True,cancel=Mock());self.manager.pi_execution=job
        self.a.motion_request('hold');job.cancel.assert_called_once_with('hold',self.a)
        with self.assertRaisesRegex(ValueError,'실행 중'):self.b.motion_request('move',[self.b.target])
        self.a.session.request.assert_not_called();self.b.session.request.assert_not_called()
        job.busy=False


class ExecutionTransportTests(unittest.TestCase):
    def test_active_job_blocks_configuration_but_allows_immediate_hold(self):
        link=object.__new__(RemoteLink);link.execution_owner=SimpleNamespace(busy=True);link.error=None;link.lease='lease';link.http=Mock(return_value={})
        for method,args in [('configure',{}),('command',{'action':'arm'}),('command',{'action':'play'})]:
            with self.assertRaisesRegex(ValueError,'실행 중'):link.rpc(method,args)
        link.http.assert_not_called();link.rpc('command',{'action':'hold'});link.http.assert_called_once()

    def test_observer_camera_never_starts_changes_or_stops_pi_capture(self):
        link=SimpleNamespace(error=None,generation=0,camera_rpc=Mock())
        camera=RemoteCameraSession(link,observer=True)
        def frame():
            camera.stop.set()
            return {'running':False,'generation':0,'server_now':time.monotonic(),'error':None}
        camera.transport=SimpleNamespace(request=frame,close=Mock());camera.run()
        link.camera_rpc.assert_not_called();self.assertFalse(camera.running)

    def test_stage_and_inspection_results_have_visible_korean_labels(self):
        self.assertEqual(gui.status_text('B_STAGE_DONE:LOWER'),'하단 완료')
        self.assertIn('안착 검사 통과',gui.status_text('B_INSPECTION_PASS:step'))
        self.assertIn('리니어',gui.status_text('B_LINEAR_STARTED:target_mm=100'))


class BlockingInput:
    def __init__(self):self.queue=queue.Queue()
    def __iter__(self):return self
    def __next__(self):
        item=self.queue.get()
        if item is None:raise StopIteration
        return item


class PiRunnerAdapterTests(unittest.TestCase):
    def test_gui_snapshot_uses_linear_cli_events_and_does_not_acquire_arm_lease(self):
        import linear_client
        originals={name:getattr(cli,name) for name in ('Link','Control','Status','load_job','ROOT')}
        old_signals={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
        stream=BlockingInput();links={};linear=FakeLinear();linear.open=Mock();linear.close=Mock()
        class SimulatedLink(cli.Link):
            def __init__(self,arm):
                super().__init__(arm);self.device=FakeLink();links[arm]=self
            def post(self,endpoint,body=None,timeout=5):
                if endpoint=='/acquire':raise AssertionError('GUI lease must not be replaced')
                if endpoint=='/detach':raise AssertionError('GUI lease must remain connected')
                if endpoint=='/state':return {'instance':self.arm,'detached':False,'follower':self.device.state()}
                if body['method']=='execution_state':return self.device.state()
                return self.device.rpc(body['method'],body['args'])
        request={'arm':'arm3','episode':{'id':'a'*32,'name':'selected','robot_id':'arm3','product_type':'B',
                 'steps':[{'id':'b'*32,'name':'renamed','ticks':{}}],'completion_events':{'PLACE':'b'*32}},
                 'links':{key:{'port':8765,'token':'test','lease':'gui-'+key,'instance':key} for key in ('arm2','arm3')},'apply_jig':True}
        try:
            with tempfile.TemporaryDirectory() as directory:
                cli.ROOT=Path(directory);(cli.ROOT/'data').mkdir();cli.Link=SimulatedLink
                job=(cli.ROOT/'data',request['episode'],{},100.,20.)
                receiver.install_adapters(cli,request,job,stream)
                with patch.object(linear_client,'LinearClient',return_value=linear):
                    self.assertEqual(cli.main(['load_b','--no-ros']),0)
                self.assertIn(('ensure',{'target_mm':1.5}),linear.calls)
                self.assertEqual(links['arm3'].lease,'gui-arm3')
                records=list((cli.ROOT/'data/episode-cli-runs').glob('*.json'));self.assertEqual(len(records),1)
                events=json.loads(records[0].read_text())['events'];states=[e['status'] for e in events]
                self.assertIn('B_STAGE_DONE:PLACE',states);self.assertEqual(states[-1],'B_DONE')
                from work_monitor import Events
                read,errors=Events(cli.ROOT).read_new();self.assertFalse(errors);self.assertEqual(read,events)
                self.assertTrue(any(args.get('action')=='play' for method,args in links['arm3'].device.calls))
        finally:
            stream.queue.put(None)
            for key,value in originals.items():setattr(cli,key,value)
            for sig,handler in old_signals.items():signal.signal(sig,handler)

    def test_taught_mode_preserves_exact_ticks_without_pi_ik(self):
        originals={name:getattr(cli,name) for name in ('Link','Control','Status','load_job')}
        try:
            receiver.install_adapters(cli,{'apply_jig':False},None,BlockingInput())
            link=cli.Link('arm2');steps=[{'ticks':{'shoulder_pan':2048},'jig_id':'pallet'}]
            with patch.object(link,'post') as post:
                self.assertEqual(link.rpc('plan',{'steps':steps,'current':{}}),{'plan':[{'ticks':steps[0]['ticks'],'corrected':False}]})
                post.assert_not_called()
        finally:
            for name,value in originals.items():setattr(cli,name,value)

    def test_cancelled_stdin_prevents_subsequent_arm_or_play_requests(self):
        originals={name:getattr(cli,name) for name in ('Link','Control','Status','load_job')}
        old_hup=signal.getsignal(signal.SIGHUP)
        stream=BlockingInput();request={'apply_jig':True,'episode':{'product_type':'A'}}
        try:
            receiver.install_adapters(cli,request,None,stream);control=cli.Control()
            stream.queue.put('cancel\n');self.assertTrue(control.shutdown.wait(1))
            link=cli.Link('arm2')
            with patch.object(link,'post') as post:
                for action in ('arm','move','play'):
                    with self.assertRaises(cli.ShutdownRequested):link.rpc('command',{'action':action})
                post.assert_not_called()
        finally:
            stream.queue.put(None);signal.signal(signal.SIGHUP,old_hup)
            for name,value in originals.items():setattr(cli,name,value)


class ExecutionLeaseTests(unittest.TestCase):
    from tests.test_remote import RemoteTests as _Fixture
    setUp=_Fixture.setUp
    tearDown=_Fixture.tearDown
    rpc=_Fixture.rpc

    def test_execution_state_is_read_only_and_rejects_expired_ui_lease(self):
        before=self.runtime.last_heartbeat
        self.assertEqual(self.rpc('execution_state'),{'ok':True,'value':None})
        self.assertEqual(self.runtime.last_heartbeat,before)
        self.runtime.detached=True
        with self.assertRaisesRegex(ValueError,'끊겨'):self.rpc('execution_state')
        self.assertIsNone(self.runtime.session)
