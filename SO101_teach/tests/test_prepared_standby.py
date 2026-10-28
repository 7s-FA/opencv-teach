"""Offline regression: no serial ports, USB devices, ROS or SSH are opened."""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import fcntl
import json
import threading
import time
import unittest
from unittest.mock import Mock,patch

import numpy as np
from so101_teach.prepared_standby import Standby,InspectorCache,RemotePreparedInspector
from so101_teach.remote_server import Runtime,Server,Handler
from so101_teach.idle_release import IdleRelease
from so101_teach.shared_capture import SharedCapture
from tests.test_episode_cli import cli
from standby_preparation import WorkcellPreparation


class StandbyClockTests(unittest.TestCase):
    def test_completion_starts_three_minutes_and_new_completion_renews(self):
        s=Standby();g=s.begin();self.assertFalse(s.expired(10000,{}))
        s.complete(g,20);self.assertFalse(s.expired(199.999,{}));self.assertTrue(s.expired(200,{}))
        g=s.begin();self.assertFalse(s.expired(500,{}));s.complete(g,500)
        self.assertEqual(s.until,680)
    def test_status_and_duplicate_completion_never_extend_deadline(self):
        s=Standby();g=s.begin();s.complete(g,10)
        self.assertEqual(s.status(20)['remaining_seconds'],170)
        with self.assertRaises(ValueError):s.complete(g,50)
        self.assertEqual(s.until,190)
    def test_cancel_and_new_owner_reject_late_warmup(self):
        s=Standby();old=s.begin();s.cancel()
        with self.assertRaises(ValueError):s.complete(old,10)
        new=s.begin()
        with self.assertRaises(ValueError):s.complete(old,20)
        s.complete(new,30);self.assertEqual(s.until,210)
    def test_client_disconnect_restart_and_stop_fence_expire_readiness(self):
        h={'epoch':'pi1','generation':3,'seen':{'pc':10}}
        s=Standby();g=s.begin('pc',h);s.complete(g,10)
        self.assertFalse(s.expired(12.99,h));self.assertTrue(s.expired(13,h))
        for changed in ({**h,'epoch':'pi2'},{**h,'generation':4},{}):
            self.assertTrue(s.expired(10,changed))

    def test_corrupt_health_is_not_a_lease_and_never_breaks_the_watcher(self):
        s=Standby();g=s.begin('pc',{'epoch':'e','generation':0});s.complete(g,10)
        for value in (None,[],{'seen':[]},{'seen':{'pc':'bad'}},{'seen':{'pc':float('nan')}}):
            self.assertFalse(s.valid_client(value,10))


class RuntimeStandbyTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.now=[10.];self.clock=patch('so101_teach.remote_server.time.monotonic',side_effect=lambda:self.now[0]);self.clock.start();self.addCleanup(self.clock.stop)
        r=self.r=Runtime.__new__(Runtime);r.data_dir=self.root/'data';r.robot_id='arm2'
        r.lock=threading.RLock();r.control_lock=threading.RLock();r.standby=Standby();r.idle_release=IdleRelease()
        r.handoff_ready=False;r.stop_generation=0;r.lease='lease';r.detached=False;r.last_heartbeat=10
        r.profile={'robot_id':'arm2'};r.compute=None;r.worker=None;r.requests=OrderedDict();r.event=Mock()
        self.sample=SimpleNamespace(monotonic=10,ticks=dict.fromkeys('abcdef',2048),calibration_matches=True,
            telemetry={k:{'torque':1,'moving':0} for k in 'abcdef'},fresh=lambda *a:True)
        r.session=SimpleNamespace(running=True,error=None,state='HOLD',latest=self.sample,
            command_pending=threading.Event(),program_active=threading.Event(),hold_requested=threading.Event(),request=Mock())
        r.camera=SimpleNamespace(running=True,error=None,preview_frame=(np.zeros((2,3,3),np.uint8),10),
            observation=(np.zeros((2,3,3),np.uint8),{'vision_ready':True},10),close=Mock())
        prepared=SimpleNamespace(ready=threading.Event(),closed=threading.Event(),error=None,profile=r.profile)
        prepared.ready.set();r.inspector_cache=SimpleNamespace(prepared=prepared,clear=Mock())
    def ready(self,client=None):
        g=self.r.call('standby_begin',{'client_id':client})['generation']
        self.r.call('standby_complete',{'generation':g});return g
    def test_detach_preserves_camera_and_models_and_does_not_release_torque(self):
        self.ready();self.r.detach('lease',retain_prepared=True)
        self.r.camera.close.assert_not_called();self.r.inspector_cache.clear.assert_not_called()
        self.r.session.request.assert_not_called();self.assertTrue(self.r.detached)
    def test_expiry_closes_resources_then_requires_a_fresh_stable_interval(self):
        self.ready();self.now[0]=190;self.r.camera.preview_frame=(self.r.camera.preview_frame[0],190);self.r.camera.observation=(*self.r.camera.observation[:2],190);self.r.poll_standby()
        self.r.camera.close.assert_called_once();self.r.inspector_cache.clear.assert_called_once()
        self.r.session.request.assert_not_called()
        self.assertIs(self.r.idle_release.session,self.r.session)
        for at in (190,192.99,193):
            self.sample.monotonic=at;self.r.idle_release.poll(self.r.session,at)
        self.r.session.request.assert_called_once_with('release')
    def test_expiring_peer_can_release_after_shared_camera_owner_closes(self):
        self.ready();self.now[0]=190;self.r.camera.running=False
        self.r.poll_standby()
        self.assertIs(self.r.idle_release.session,self.r.session)
        self.assertFalse(self.r.session.hold_requested.is_set())
        self.r.session.request.assert_not_called()
    def test_common_completion_time_prevents_different_arm_deadlines(self):
        generation=self.r.standby.begin();self.now[0]=10.4
        self.r.call('standby_complete',{'generation':generation,'completed_at':10.})
        self.assertEqual(self.r.standby.until,190.)
        generation=self.r.standby.begin()
        for invalid in (20.,-100.,float('nan'),True):
            with self.assertRaises(ValueError):self.r.call('standby_complete',{'generation':generation,'completed_at':invalid})
    def test_workcell_lock_defers_expiry_during_new_command(self):
        self.ready();self.now[0]=250
        with (self.root/'data/episode-cli.lock').open('a') as work:
            fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB);self.r.poll_standby()
            self.r.camera.close.assert_not_called()
            self.r.call('standby_begin',{})
        self.r.poll_standby();self.r.camera.close.assert_not_called()
    def test_disconnect_preserves_hold_and_never_schedules_torque_release(self):
        path=self.root/'data/standby-supervision.json'
        path.write_text(json.dumps({'epoch':'pi','generation':0,'seen':{'pc':10}}))
        self.ready('pc');self.now[0]=13;self.r.poll_standby()
        self.assertTrue(self.r.session.hold_requested.is_set())
        self.assertIsNone(self.r.idle_release.session);self.r.session.request.assert_not_called()
    def test_expiry_while_moving_requests_hold_without_torque_off(self):
        self.ready();self.r.session.state='MOVING';self.now[0]=190;self.r.poll_standby()
        self.assertTrue(self.r.session.hold_requested.is_set());self.assertIsNone(self.r.idle_release.session)
        self.r.session.request.assert_not_called()
    def test_idle_stop_is_immediate_and_late_completion_cannot_revive(self):
        g=self.ready();reply=self.r.standby_control({'operation':'cancel'})
        self.assertTrue(reply['cancelled']);self.assertTrue(self.r.session.hold_requested.is_set())
        self.assertFalse(self.r.handoff_ready)
        with self.assertRaises(ValueError):self.r.call('standby_complete',{'generation':g})
        self.assertIsNone(self.r.standby.until)
    def test_gui_snapshot_claim_cancels_bounded_workcell_cache(self):
        self.ready();self.r.call('claim_episode',{})
        self.assertIsNone(self.r.standby.until);self.assertFalse(self.r.standby.active)
        self.r.camera.close.assert_called_once();self.r.inspector_cache.clear.assert_called_once()
    def test_failure_disconnect_cancels_even_an_already_committed_standby(self):
        self.ready();self.r.detach('lease')
        self.assertIsNone(self.r.standby.until)
        self.r.camera.close.assert_called_once();self.r.inspector_cache.clear.assert_called_once()
    def test_non_prepared_detach_cleans_resources(self):
        self.r.standby.begin();self.r.detach('lease')
        self.r.camera.close.assert_called_once();self.r.inspector_cache.clear.assert_called_once()
    def test_stale_camera_or_missing_torque_cannot_be_ready(self):
        g=self.r.standby.begin();self.now[0]=12
        with self.assertRaisesRegex(ValueError,'카메라'):self.r.call('standby_complete',{'generation':g})
        self.now[0]=10;self.sample.telemetry['a']['torque']=0
        with self.assertRaisesRegex(ValueError,'토크'):self.r.call('standby_complete',{'generation':g})
        self.assertIsNone(self.r.standby.until)
    def test_request_replay_and_read_only_query_do_not_renew(self):
        g=self.r.standby.begin();body={'lease':'lease','id':'complete','method':'standby_complete','args':{'generation':g}}
        self.assertTrue(self.r.rpc(body)['ok']);until=self.r.standby.until
        self.now[0]=11;self.assertTrue(self.r.rpc(body)['ok'])
        self.r.standby_control({'operation':'status'});self.assertEqual(self.r.standby.until,until)
    def test_linear_completion_refreshes_existing_resources_without_actuator_commands(self):
        self.ready();self.now[0]=100;self.r.camera.preview_frame=(self.r.camera.preview_frame[0],100)
        self.r.camera.observation=(*self.r.camera.observation[:2],100)
        self.r.call('standby_refresh',{});self.assertEqual(self.r.standby.until,280)
        self.r.session.request.assert_not_called();self.r.camera.close.assert_not_called()
    def test_cold_linear_command_does_not_create_motor_or_camera_readiness(self):
        self.assertEqual(self.r.call('standby_refresh',{}),{'ready':False})
        self.r.session.request.assert_not_called();self.assertIsNone(self.r.standby.until)
    def test_device_failure_invalidates_readiness_and_holds_without_release(self):
        self.ready();self.r.camera.error='capture stopped';self.now[0]=10.2;self.r.poll_standby()
        self.assertIsNone(self.r.standby.until);self.assertIsNone(self.r.idle_release.session)
        self.assertTrue(self.r.session.hold_requested.is_set())
    def test_idle_stop_does_not_report_verified_stop_from_unknown_motion(self):
        self.ready();self.sample.telemetry['a']['moving']=1
        reply=self.r.standby_control({'operation':'cancel'})
        self.assertTrue(reply['cancelled']);self.assertFalse(reply['stopped'])
    def test_configuration_edit_invalidates_readiness_without_releasing_torque(self):
        (self.root/'data/profile.json').write_text('{}')
        self.ready();(self.root/'data/profile.json').write_text('{"changed":true}')
        self.now[0]=11;self.r.poll_standby()
        self.assertIsNone(self.r.standby.until);self.assertIsNone(self.r.idle_release.session)
        self.assertTrue(self.r.session.hold_requested.is_set())
        self.r.camera.close.assert_called_once()
    def test_resident_inspection_keeps_pixels_and_discards_response_after_stop(self):
        from so101_teach.pc_compute import encode,decode
        frame=np.random.default_rng(5).integers(0,256,(12,16,3),dtype=np.uint8)
        received=[]
        def inspect(frame,**kwargs):
            received.append(frame.copy());kwargs['evidence'](frame,[],[])
            return [{'result':'PASS'}]
        self.r.inspector_cache.prepared.borrow=lambda:SimpleNamespace(inspect=inspect)
        args={'profile':self.r.profile,'method':'inspect','payload':encode({'frame':frame,'anchors':[],
              'profile':self.r.profile,'context_anchors':None,'inference':None})}
        result=decode(self.r.inspection_call(args));self.assertEqual(result['rows'],[{'result':'PASS'}])
        np.testing.assert_array_equal(received[0],frame)
        def stopped(frame,**kwargs):self.r.stop_generation+=1;return [{'result':'PASS'}]
        self.r.inspector_cache.prepared.borrow=lambda:SimpleNamespace(inspect=stopped)
        with self.assertRaisesRegex(ValueError,'응답 폐기'):self.r.inspection_call(args)
    def test_cancelled_inspector_cannot_restart_from_an_inspection_read(self):
        self.r.inspector_cache.prepared=None
        with self.assertRaisesRegex(ValueError,'취소'):
            self.r.inspection_call({'profile':self.r.profile,'method':'predict_all','payload':{}})


class SharedCaptureTests(unittest.TestCase):
    def test_two_arms_open_one_device_and_share_exact_pixels_and_capture_time(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'arm3-runtime').mkdir()
            owner_runtime=SimpleNamespace(data_dir=root,robot_id='arm2')
            peer_runtime=SimpleNamespace(data_dir=root/'arm3-runtime',robot_id='arm3')
            frame=np.random.default_rng(1).integers(0,256,(24,32,3),dtype=np.uint8)
            native=Mock();native.isOpened.return_value=True;native.read.return_value=(True,frame)
            with patch('cv2.VideoCapture',return_value=native) as open_device:
                owner=SharedCapture(owner_runtime,'/fake/camera',32,24)
                self.addCleanup(owner.release)
                self.assertTrue(owner.read()[0]);at=owner.captured_at
                owner_runtime.camera=SimpleNamespace(source='/fake/camera',width=32,height=24,running=True,
                    capture=owner,preview_frame=(frame,at))
                server=Server(('127.0.0.1',0),Handler);server.runtime=owner_runtime;server.token='test'
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    (root/'remote-server.json').write_text(json.dumps({'port':server.server_port}))
                    (root/'remote-token').write_text('test')
                    reader=SharedCapture(peer_runtime,'/fake/camera',32,24)
                    try:
                        ok,actual=reader.read();self.assertTrue(ok);np.testing.assert_array_equal(frame,actual)
                        self.assertEqual(reader.captured_at,at);self.assertEqual(reader.read(),(False,None))
                        open_device.assert_called_once()
                    finally:reader.release()
                finally:server.shutdown();server.server_close();thread.join(1)
    def test_shared_reader_rejects_other_resolution_and_stale_capture(self):
        from so101_teach.shared_capture import capture_frame
        capture=SharedCapture.__new__(SharedCapture);capture.cap=object()
        r=SimpleNamespace(camera=SimpleNamespace(source='s',width=32,height=24,running=True,capture=capture,
            preview_frame=(np.zeros((24,32,3),np.uint8),time.monotonic()-2)))
        with self.assertRaisesRegex(ValueError,'설정'):capture_frame(r,{'source':'s','width':64,'height':24})
        with self.assertRaisesRegex(ValueError,'지연'):capture_frame(r,{'source':'s','width':32,'height':24})


class InspectorRetentionTests(unittest.TestCase):
    def test_same_profile_reuses_instance_and_profile_change_discards_old_one(self):
        cache=InspectorCache();made=[]
        def factory(profile,**kw):
            p=SimpleNamespace(closed=threading.Event(),close=Mock(),profile=profile);made.append(p);return p
        with patch('so101_teach.episode_inspection_runtime.PreparedInspector',side_effect=factory):
            a=cache.prepare({});self.assertIs(cache.prepare({}),a)
            b=cache.prepare({'tcp':[1,2,3]});self.assertIsNot(a,b);self.assertTrue(a.closed.is_set())
            cache.clear();self.assertTrue(b.closed.is_set())
        self.assertEqual(len(made),2)
    def test_proxy_close_ends_only_its_evidence_and_never_closes_resident_model(self):
        link=Mock();prepared=RemotePreparedInspector(link,{'robot_id':'arm2'});visual=prepared.visual=Mock()
        prepared.close();visual.close.assert_called_once()
        self.assertEqual([call.args[0] for call in link.rpc.call_args_list],['inspection_prepare'])
        self.assertEqual(prepared.borrow().predict_all(np.zeros((2,2,3),np.uint8),{}),[])
    def test_resident_rpc_preserves_real_saved_frame_geometry_results(self):
        import cv2
        from so101_teach.domain import ROOT
        from so101_teach.inspection_geometry import anchors_for
        from so101_teach.shape_inspection import ShapeInspector
        from so101_teach.pc_compute import encode
        folder=ROOT/'tests/fixtures/part-inspection'
        fixture=json.loads((folder/'fixture.json').read_text());profile=fixture['profile']
        profile['part_inspection']={'method':'opencv'}
        frame=cv2.imread(str(folder/'insert-added.jpg'))
        reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
        anchors,_=anchors_for(frame,{},{},{},profile,reference,'리니어 조립','전체',fixture['placement'],'명령 위치')
        expected=ShapeInspector().inspect(frame,anchors,profile)
        runtime=Runtime.__new__(Runtime);runtime.profile=profile;runtime.compute=None
        runtime.stop_generation=0;runtime.lease='job';runtime.detached=False;runtime.inspector_cache=InspectorCache()
        link=SimpleNamespace(compute=None,rpc=lambda method,args=None:runtime.call(method,args or {}))
        try:
            proxy=RemotePreparedInspector(link,profile)
            actual=proxy.borrow().inspect(frame,anchors,profile)
            self.assertEqual(encode(actual),encode(expected))
            self.assertEqual([row['state'] for row in actual],['insert_added','insert_added'])
            proxy.close();self.assertIsNotNone(runtime.inspector_cache.prepared)
        finally:runtime.inspector_cache.clear()
    def test_response_after_stop_is_discarded(self):
        stop=threading.Event();link=Mock();p=RemotePreparedInspector(link,{})
        def late(*args):stop.set();return [{'class_id':1}]
        link.rpc.side_effect=late;inspector=p.borrow();inspector.stop=stop
        self.assertEqual(inspector.predict_all(np.zeros((2,2,3),np.uint8),{}),[])


class WorkcellPreparationTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.links={}
        for arm in ('arm2','arm3'):
            def rpc(method,args=None,arm=arm):
                self.events.append((arm,method,args))
                return {'generation':1,'ready':True}
            self.links[arm]=SimpleNamespace(arm=arm,rpc=rpc)
        records={arm:{'safe_ticks':dict.fromkeys('abcdef',2000),'bundle':{'profile':{'robot_id':arm}}} for arm in self.links}
        self.active=Mock();self.emergency=Mock();self.control=SimpleNamespace(emergency=self.emergency)
        self.job=SimpleNamespace(link=self.links['arm2'],peer=self.links['arm3'],standby_records=records,
            args=SimpleNamespace(supervision_client_id=None),control=self.control,runner=self.active,linear=object(),
            status=SimpleNamespace(emit=Mock()),slide_only=True,pc_command=False,target=100,arm='arm2')
        def runner(link,emit,control,*args,**kw):
            r=Mock();control.emergency=r.emergency;return r
        self.context=SimpleNamespace(Runner=runner,recovery_context=lambda:object())
        self.p=WorkcellPreparation(self.job,self.context)
    def test_build_slide_prepares_both_arms_and_completes_without_three_minute_sleep(self):
        receipts={arm:{'request_id':1,'ticks':r['safe_ticks']} for arm,r in self.job.standby_records.items()}
        def prepare(*args,**kw):kw['on_started']();return receipts
        with patch('episode_recovery.prepare_chain_start',side_effect=prepare) as paired:
            self.p.begin();self.p.prepare();self.p.finish()
        self.assertTrue(self.p.committed);self.assertEqual(paired.call_count,1)
        self.active.start_linear.assert_called_once_with(100,defer_pause=True)
        self.active.wait_linear.assert_called_once()
        self.assertIs(self.control.emergency,self.emergency)
        self.assertEqual({a for a,m,_ in self.events if m=='standby_complete'},{'arm2','arm3'})
        self.assertFalse(any(m=='schedule_idle_release' for _,m,_ in self.events))
        completions=[args for _,method,args in self.events if method=='standby_complete']
        self.assertEqual(completions[0]['completed_at'],completions[1]['completed_at'])
    def test_load_slide_prepares_from_cold_and_uses_unload_target(self):
        self.job.target=1.5
        receipts={arm:{'request_id':1,'ticks':r['safe_ticks']} for arm,r in self.job.standby_records.items()}
        def prepare(*args,**kw):kw['on_started']();return receipts
        with patch('episode_recovery.prepare_chain_start',side_effect=prepare):
            self.p.begin();self.p.prepare();self.p.finish()
        self.active.start_linear.assert_called_once_with(1.5,defer_pause=True)
        self.active.wait_linear.assert_called_once()
        for arm in self.links:
            methods=[method for a,method,_ in self.events if a==arm]
            self.assertIn('inspection_prepare',methods);self.assertIn('standby_complete',methods)
            self.assertNotIn('standby_refresh',methods)

    def test_warm_clears_old_jig_acceptance_for_both_arms(self):
        self.p.warm()
        for arm in self.links:
            events=[m for a,m,_ in self.events if a==arm]
            self.assertLess(events.index('detect_freeze'),events.index('detect_clear'))
            self.assertLess(events.index('detect_clear'),events.index('inspection_prepare'))
    def test_build_slide_uses_current_recipes_and_never_restores_old_roi(self):
        self.job.control.startup_timing=None
        ticks=dict.fromkeys('abcdef',2048)
        def load(arm,letter,**kw):
            return None,{'steps':[{'safe_boundary':'start','ticks':ticks}]},{'profile':{'robot_id':arm},'jigs':{'roi':'current'}},100,120
        context=SimpleNamespace(load_job=Mock(side_effect=load),chain_start_records=cli.chain_start_records,
                                recovery_context=Mock())
        with patch('episode_recovery.workcell_records',side_effect=AssertionError('history must not be used')):
            records=WorkcellPreparation.records(self.job,context)
        self.assertEqual(context.load_job.call_count,4)
        self.assertTrue(all(r['bundle']['jigs']['roi']=='current' for r in records.values()))
    def test_partial_complete_failure_leaves_no_ready_arm_after_cleanup(self):
        original=self.links['arm3'].rpc
        def fail(method,args=None):
            if method=='standby_complete':raise RuntimeError('lost')
            return original(method,args)
        self.links['arm3'].rpc=fail
        with self.assertRaisesRegex(RuntimeError,'lost'):self.p.finish()
        self.assertFalse(self.p.committed);self.p.cancel()
        self.assertEqual({a for a,m,_ in self.events if m=='standby_cancel'},{'arm2','arm3'})
        self.assertTrue(all(not link.keep_prepared for link in self.links.values()))
    def test_warm_failure_restores_original_emergency_handler(self):
        self.context.Runner=lambda *a,**k:SimpleNamespace(prepare_camera=Mock(side_effect=ValueError('camera')))
        with self.assertRaisesRegex(ValueError,'camera'):self.p.warm()
        self.assertIs(self.control.emergency,self.emergency)


class CommandPreparationCoverageTests(unittest.TestCase):
    def test_all_ten_job_commands_validate_both_arm_preparation_without_devices(self):
        ticks=dict.fromkeys('abcdef',2048)
        episode={'name':'fixture','steps':[{'safe_boundary':'start','ticks':ticks},
            {'id':'placed','inspection':{'station':'finished','target':'pallet','expected':'cap_added'}}]}
        job=(None,episode,{'profile':{}},100,180)
        for command in cli.JOB_PLANS:
            with self.subTest(command=command),patch.object(cli,'load_job',return_value=job) as load,patch.object(cli,'Link') as link,patch('so101_teach.pc_compute.validate_profile'):
                execution=cli.JobExecution(command,cli.parse_command([command,'--check']))
                execution.load_recipes()
                self.assertEqual(set(execution.standby_records),{'arm2','arm3'})
                self.assertEqual({call.args[0] for call in load.call_args_list},{'arm2','arm3'})
                link.assert_not_called()

    def test_home_validates_current_preparation_and_passes_supervision_client(self):
        args=cli.parse_command(['prepare_home','--check'])
        with patch('episode_recovery.workcell_records'),patch.object(WorkcellPreparation,'records') as records,patch.object(cli,'Link') as link:
            self.assertEqual(cli.run_home_command('prepare_home',args),0)
            records.assert_called_once();link.assert_not_called()
        args=cli.parse_command(['prepare_home','--no-ros','--supervision-client-id','console'])
        with patch('episode_recovery.run',return_value=0) as run:
            self.assertEqual(cli.run_home_command('prepare_home',args),0)
            self.assertEqual(run.call_args.kwargs['supervision_client_id'],'console')


class StandbyControlTests(unittest.TestCase):
    def test_failed_linear_refresh_cannot_retain_resources_when_cancel_rpc_fails(self):
        job=cli.JobExecution('slide_load',cli.parse_command(['slide_load','--no-ros']))
        job.link=SimpleNamespace(lease='one',keep_prepared=True,rpc=Mock(side_effect=OSError('lost')))
        job.peer=SimpleNamespace(lease='two',keep_prepared=True,rpc=Mock(side_effect=OSError('lost')))
        job.report_rejected=Mock()
        self.assertEqual(job.failed(RuntimeError('refresh failed')),1)
        self.assertFalse(job.link.keep_prepared);self.assertFalse(job.peer.keep_prepared)
    def test_failed_first_arm_still_attempts_to_cancel_the_other(self):
        import io
        from standby_control import cancel
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for index,folder in enumerate((root/'data',root/'data/arm3-runtime')):
                folder.mkdir(parents=True,exist_ok=True)
                (folder/'remote-server.json').write_text(json.dumps({'port':5000+index}))
                (folder/'remote-token').write_text('test')
            response=io.StringIO(json.dumps({'ok':True,'value':{'cancelled':True,'stopped':True}}))
            with patch('standby_control.urlopen',side_effect=[OSError('disconnected'),response]) as call:
                with self.assertRaisesRegex(RuntimeError,'arm2'):cancel(root)
            self.assertEqual(call.call_count,2)
