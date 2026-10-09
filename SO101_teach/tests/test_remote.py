"""Remote boundaries: no motors touched; real runtime/HTTP/IK code is exercised."""
import base64,json,queue,tempfile,threading,time,unittest,uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests.fixtures import DATA,load_profile
from so101_teach.domain import JOINTS,Snapshot,EpisodeStore
from so101_teach.configuration import JigCatalog
from so101_teach.vision import PoseLatch
from so101_teach.remote_config import configuration_bundle,materialize
from so101_teach.remote_server import Runtime,Server,Handler
from so101_teach.remote_client import RemoteLink,RemoteMotionSession,RemoteCameraSession,snapshot,translate_detection
from so101_teach.motion import MotionSession,SPEED_PRESETS

class FakeMotion(MotionSession):
    def start(self):self.running=True
    def close(self):self.running=False
    def join(self,timeout=1):return not self.running

class FakeCamera:
    def set_mode(self,**mode):self.processing_enabled=mode["processing_enabled"];self.preview_fps=mode["preview_fps"]
    def __init__(self,**kw):self.running=False;self.error=None;self.observation=None
    def start(self):self.running=True
    def close(self):self.running=False
    def join(self,timeout=1):return True

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();p,c,r=load_profile();p['mode']='follower'
        self.app=SimpleNamespace(profile=p,data_dir=DATA,catalog=JigCatalog(DATA),reference=r,pose_latch=PoseLatch(),active_jig='pallet')
        self.bundle=configuration_bundle(self.app);self.runtime=Runtime(self.tmp.name,motion_factory=FakeMotion,camera_factory=FakeCamera)
        self.lease=self.runtime.acquire()['lease'];self.runtime.configure(self.bundle)
    def tearDown(self):self.runtime.close();self.tmp.cleanup()
    def rpc(self,method,args=None,key=None):return self.runtime.rpc({'lease':self.lease,'id':key or uuid.uuid4().hex,'method':method,'args':args or {}})
    def test_bundle_preserves_calibration_angles_and_stl_without_local_paths(self):
        self.assertEqual(self.runtime.cal.sha256,self.app.reference.calibration.sha256)
        self.assertEqual(self.runtime.catalog.mesh('pallet')['sha256'],self.app.catalog.mesh('pallet')['sha256'])
        self.assertTrue(str(self.runtime.catalog.items['pallet']['stl']).startswith(self.tmp.name))
        self.assertEqual(self.runtime.ref.angles(self.runtime.ref.middle),self.app.reference.angles(self.app.reference.middle))
    def test_traversal_and_missing_files_rejected_before_materializing(self):
        bad=deepcopy(self.bundle);bad['files']['../escape.json']=base64.b64encode(b'{}').decode()
        with self.assertRaises(ValueError):materialize(self.tmp.name,bad)
        bad=deepcopy(self.bundle);bad['profile']['calibration_file']='/tmp/not-a-calibration.json'
        with self.assertRaises(ValueError):materialize(self.tmp.name,bad)
    def test_three_speed_presets_apply_remotely_without_motor_commands(self):
        self.assertTrue(self.rpc('connect',{'speed':SPEED_PRESETS['보통']})['ok'])
        s=self.runtime.session;self.assertEqual(s.rate_ticks_s,350)
        for rate in (300,350,400):
            self.assertTrue(self.rpc('speed',{'rate':rate})['ok']);self.assertEqual(s.rate_ticks_s,rate)
            self.assertTrue(s.commands.empty());self.assertFalse(s.command_log);self.assertEqual(s.state,'READ_ONLY')
    def test_connect_is_read_only_and_duplicate_command_runs_once(self):
        self.assertTrue(self.rpc('connect',{'speed':SPEED_PRESETS['보통']})['ok']);s=self.runtime.session
        self.assertEqual(s.state,'READ_ONLY');self.assertTrue(s.commands.empty())
        with patch.object(s,'request',return_value=4) as request:
            first=self.rpc('command',{'action':'arm'},'one');second=self.rpc('command',{'action':'arm'},'one')
            self.assertEqual(first,second);request.assert_called_once_with('arm',None)
            with self.assertRaises(ValueError):self.rpc('command',{'action':'move'},'one')
    def test_failed_commands_are_not_replayed(self):
        with patch.object(self.runtime,'call',side_effect=ValueError('failed')) as call:
            self.assertFalse(self.rpc('connect',{'speed':SPEED_PRESETS['보통']},'one')['ok']);self.rpc('connect',{'speed':SPEED_PRESETS['보통']},'one');self.assertEqual(call.call_count,1)
    def test_expired_lease_holds_does_not_release_or_replay(self):
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});s=self.runtime.session;s.state='HOLD';self.runtime.last_heartbeat=time.monotonic()-3
        with self.assertRaises(ValueError):self.rpc('command',{'action':'arm'})
        end=time.monotonic()+1
        while not self.runtime.detached and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(s.hold_requested.is_set());self.assertFalse(s.release_requested.is_set());self.assertTrue(s.running)
        old=self.lease;self.lease=self.runtime.acquire()['lease'];self.assertNotEqual(old,self.lease);self.assertTrue(s.commands.empty())
    def test_second_controller_cannot_acquire_live_lease(self):
        with self.assertRaises(ValueError):self.runtime.acquire()
    def test_hold_release_bypass_slow_plan_lock(self):
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});done=threading.Event()
        with self.runtime.lock:
            t=threading.Thread(target=lambda:(self.rpc('command',{'action':'release'}),done.set()));t.start();self.assertTrue(done.wait(.5))
        t.join();self.assertTrue(self.runtime.session.release_requested.is_set())
    def test_camera_roi_change_restarts_vision_and_invalidates_old_measurement(self):
        self.runtime.start_camera();old=self.runtime.camera;self.bundle['jigs']['pallet']['roi']=[.1,.1,.8,.8]
        generation=self.runtime.generation;self.runtime.configure(self.bundle)
        self.assertFalse(old.running);self.assertTrue(self.runtime.camera.running);self.assertGreater(self.runtime.generation,generation)
        self.assertEqual(self.runtime.catalog.items['pallet']['roi'],[.1,.1,.8,.8])
    def test_candidate_confirmation_time_transfers_and_invalid_value_is_atomic(self):
        self.app.pose_latch.configure(10,acquisition_seconds=10.,attempts_limit=5)
        bundle=configuration_bundle(self.app);self.runtime.configure(bundle)
        self.assertEqual(self.runtime.detector.latches['pallet'].stability.seconds,10.)
        self.assertEqual(self.runtime.detector.latches['pallet'].stability.attempts_limit,5)
        old=self.runtime.detector;generation=self.runtime.generation;bundle['acquisition_seconds']=-1
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        self.assertIs(self.runtime.detector,old);self.assertEqual(self.runtime.generation,generation)
        bundle['acquisition_seconds']=3.;bundle['hold_seconds']=11
        with self.assertRaises(ValueError):self.runtime.configure(bundle)
        self.assertIs(self.runtime.detector,old);self.assertEqual(self.runtime.generation,generation)
    def test_remote_fixed_plan_and_episode_persistence_use_exact_ticks(self):
        e=self.runtime.store.new('remote');e['steps']=[self.runtime.store.step(self.runtime.ref.middle,'fixed')]
        self.assertTrue(self.rpc('save_episode',{'episode':e})['ok']);self.assertEqual(self.runtime.store.entries()[0][1],e)
        result=self.rpc('plan',{'steps':e['steps'],'current':None});self.assertTrue(result['ok'],result)
        self.assertEqual(result['value']['plan'][0]['ticks'],self.runtime.ref.middle)
        self.rpc('delete_episode',{'id':e['id']});self.assertFalse(self.runtime.store.entries())
    def test_geometry_plan_accepts_valid_inspection_without_episode_metadata(self):
        step=self.runtime.store.step(self.runtime.ref.middle,'fixed')
        step['inspection']=dict(station='carrier',target='insert',expected='present',timeout_seconds=5)
        before=deepcopy(step)
        result=self.rpc('plan',{'steps':[step]})
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['value']['plan'][0]['ticks'],step['ticks'])
        self.assertEqual(step,before)
        # Saving/executing a full episode still requires an explicit product.
        ep=self.runtime.store.new();ep['steps']=[step]
        self.assertFalse(self.rpc('save_episode',{'episode':ep})['ok'])
        bad=deepcopy(step);bad['inspection']['timeout_seconds']=0
        self.assertFalse(self.rpc('plan',{'steps':[bad]})['ok'])
        bad=deepcopy(step);bad['ticks']['gripper']='invalid'
        self.assertFalse(self.rpc('plan',{'steps':[bad]})['ok'])

    def test_corrected_plan_matches_same_engine_locally(self):
        from so101_teach.geometry import corrected_step
        ep=self.runtime.store.new();from so101_teach.domain import ROOT,read_json
        step=self.runtime.store.step(read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks'],'follow')
        mesh=self.runtime.catalog.mesh('pallet');step['jig_id']='pallet'
        step['jig_reference']={'pose':[228.,-138.,67.],'symmetry_deg':90,'stl_sha256':mesh['sha256']}
        now=deepcopy(step['jig_reference']);now['pose'][0]+=2
        expected=corrected_step(self.runtime.kin,step,now,mesh['sha256'])
        result=self.rpc('plan',{'steps':[step],'current':{'pallet':now}});self.assertTrue(result['ok'],result)
        self.assertEqual(result['value']['plan'],[expected])
    def test_snapshot_time_translation_is_conservative(self):
        data={'role':'follower','ticks':self.runtime.ref.middle,'telemetry':{},'monotonic':90.,'wall_time':1.,'calibration_sha256':self.runtime.cal.sha256,'calibration_matches':True,'port':'pi'}
        self.assertEqual(snapshot(data,10.).monotonic,100.)
        self.assertEqual(translate_detection({'by_jig':{'pallet':{'pose_measured_at':90.,'hold_remaining_s':10}}},10.)['by_jig']['pallet'],{'pose_measured_at':100.,'hold_remaining_s':10})
    def test_real_http_auth_and_client_never_open_local_bus(self):
        server=Server(('127.0.0.1',0),Handler);server.runtime=self.runtime;server.token='test-only';thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        link=RemoteLink({'host':'localhost','user':'robot'});link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token;link.lease=self.lease
        try:
            with patch('so101_teach.devices.new_bus',side_effect=AssertionError('local USB prohibited')):
                proxy=RemoteMotionSession(link,'pi',self.runtime.cal);proxy.start();self.assertTrue(proxy.running);self.assertEqual(self.runtime.session.state,'READ_ONLY')
                camera=RemoteCameraSession(link);camera.start();time.sleep(.08);camera.close();camera.join(2)
                proxy.close();self.assertFalse(self.runtime.session.running)
            link.token='bad'
            from urllib.error import HTTPError
            with self.assertRaises(HTTPError):link.http('/health',{})
        finally:server.shutdown();server.server_close()
    def test_pc_leader_samples_are_converted_with_follower_limits_on_pi(self):
        from so101_teach.remote_server import LeaderBridge
        from so101_teach.motion import leader_target
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});self.runtime.leader=LeaderBridge(self.runtime.cal)
        bridge=self.runtime.leader;sample={'ticks':self.runtime.ref.middle.copy(),'pi_monotonic':time.monotonic()-.03,'wall_time':time.time(),'calibration_sha256':self.runtime.cal.sha256,'calibration_matches':True,'port':'PC USB'}
        self.assertTrue(self.runtime.leader_sample({'lease':self.lease,'sequence':1,'sample':sample})['accepted'])
        targets=leader_target(bridge.latest.ticks,bridge.calibration,self.runtime.cal)
        self.assertEqual(targets,sample['ticks'])
        self.assertFalse(self.runtime.leader_sample({'lease':self.lease,'sequence':1,'sample':sample})['accepted'])
        sample['pi_monotonic']=time.monotonic()-.6
        latest=bridge.latest;self.assertFalse(self.runtime.leader_sample({'lease':self.lease,'sequence':2,'sample':sample})['accepted']);self.assertIs(bridge.latest,latest)
        sample['pi_monotonic']=time.monotonic();sample['calibration_matches']=False
        self.assertFalse(self.runtime.leader_sample({'lease':self.lease,'sequence':3,'sample':sample})['accepted']);self.assertFalse(bridge.latest.calibration_matches)
    def test_pc_leader_stream_uses_receipt_clock_anchor_and_stops_on_loss(self):
        from so101_teach.remote_server import LeaderBridge
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});self.runtime.leader=LeaderBridge(self.runtime.cal)
        server=Server(('127.0.0.1',0),Handler);server.runtime=self.runtime;server.token='test';threading.Thread(target=server.serve_forever,daemon=True).start()
        link=RemoteLink({'host':'localhost','user':'robot'});link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token;link.lease=self.lease
        now=time.monotonic()
        # Inject different PC/Pi monotonic origins (Pi = PC + 5000 seconds).
        link.clock_anchor=(now-.02,now-5000)
        pc_sample=Snapshot('leader',self.runtime.ref.middle.copy(),{},now-5000-.01,time.time(),self.runtime.cal.sha256,True,'PC')
        reader=SimpleNamespace(running=True,latest=pc_sample,close=MockClose())
        link.leader_client=reader
        try:
            with patch.object(Snapshot,'fresh',return_value=True):
                stream=threading.Thread(target=link.poll_leader,daemon=True);stream.start()
                end=time.monotonic()+.3
                while self.runtime.leader.latest is None and time.monotonic()<end:time.sleep(.005)
                received=self.runtime.leader.latest;self.assertIsNotNone(received)
                self.assertAlmostEqual(received.monotonic,now-.03,places=5);self.assertEqual(received.ticks,pc_sample.ticks)
                link.fail('network stopped');self.assertTrue(reader.close.called)
                link.stop.set();stream.join(1)
        finally:link.stop.set();server.shutdown();server.server_close()

    def test_follower_reconnect_rebinds_existing_pc_leader_bridge(self):
        self.bundle['profile']['mode']='leader';self.bundle['profile']['leader']={'port':'PC USB','calibration_file':self.bundle['profile']['calibration_file']}
        self.runtime.configure(self.bundle);self.rpc('connect',{'speed':SPEED_PRESETS['보통']});old=self.runtime.session;bridge=self.runtime.leader;old.close()
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});self.assertIsNot(self.runtime.session,old);self.assertIs(self.runtime.leader,bridge);self.assertIsNotNone(self.runtime.session.leader_provider)

    def test_explicit_close_allows_immediate_reconnect_and_rejects_old_owner(self):
        old=self.lease;self.runtime.detach(old);new=self.runtime.acquire()['lease'];self.assertNotEqual(old,new)
        with self.assertRaises(ValueError):self.rpc('connect',{'speed':SPEED_PRESETS['보통']})
        self.lease=new;self.assertTrue(self.rpc('connect',{'speed':SPEED_PRESETS['보통']})['ok'])

class MockClose:
    called=False
    def __call__(self):self.called=True

class CameraIdleRuntimeTests(unittest.TestCase):
    setUp=RemoteTests.setUp
    tearDown=RemoteTests.tearDown
    rpc=RemoteTests.rpc
    def test_detach_closes_camera_without_releasing_motor_torque(self):
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']});s=self.runtime.session;s.state='HOLD';self.runtime.start_camera();camera=self.runtime.camera
        self.runtime.detach(self.lease)
        self.assertFalse(camera.running);self.assertTrue(s.running);self.assertFalse(s.release_requested.is_set())
    def test_lost_pc_heartbeat_stops_unused_camera(self):
        self.runtime.start_camera();camera=self.runtime.camera;self.runtime.last_heartbeat=time.monotonic()-3
        end=time.monotonic()+1
        while not self.runtime.detached and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(self.runtime.detached);self.assertFalse(camera.running)
    def test_reopen_waits_for_stopping_camera_without_second_usb_owner(self):
        self.runtime.start_camera();camera=self.runtime.camera;camera.stop=threading.Event();camera.stop.set()
        camera.join=lambda timeout:None
        with self.assertRaisesRegex(ValueError,'해제 중'):self.runtime.start_camera()
        self.assertIs(self.runtime.camera,camera)
