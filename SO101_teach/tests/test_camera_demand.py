import base64,threading,time,unittest
from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
import cv2,numpy as np
from so101_teach.devices import CameraSession
from so101_teach.remote_client import RemoteCameraSession,RemoteLink
from so101_teach.remote_server import Runtime
from tests import test_ui as fixtures

class CameraDemandTests(unittest.TestCase):
    def test_pause_drops_inflight_detection_and_resume_accepts_only_new_frame(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def process(frame):
            entered.set();release.wait(2);return {'selected':frame}
        c=CameraSession(0,processor=process);c.pending_frame=('old',10.)
        worker=threading.Thread(target=c.process_frames);worker.start();c.frame_ready.set()
        try:
            self.assertTrue(entered.wait(1));c.set_mode(processing_enabled=False,preview_fps=5)
            release.set();time.sleep(.05);self.assertIsNone(c.observation)
            c.set_mode(processing_enabled=True,preview_fps=10)
            c.pending_frame=('new',20.);c.frame_ready.set()
            deadline=time.monotonic()+1
            while c.observation is None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(c.observation,('new',{'selected':'new'},20.))
        finally:c.close();release.set();worker.join(2)
    def test_paused_capture_drains_device_but_publishes_at_five_fps_without_detection(self):
        clock=[100.];reads=[0];seen=[];process=Mock()
        c=CameraSession(0,processor=process);c.set_mode(processing_enabled=False,preview_fps=5)
        def read():
            if c.preview_frame:seen.append(c.preview_frame[1])
            reads[0]+=1;clock[0]+=.101
            if reads[0]==8:c.stop.set()
            return True,np.zeros((4,4,3),np.uint8)
        cap=Mock();cap.isOpened.return_value=True;cap.read.side_effect=read
        with patch.object(cv2,'VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',side_effect=lambda:clock[0]),patch.object(c.stop,'wait',return_value=False):c.run()
        process.assert_not_called();self.assertEqual(reads[0],8);self.assertEqual(len(set(seen)),4)
        self.assertIsNone(c.observation);self.assertIsNone(c.preview_observation[1]['selected']);self.assertEqual(c.preview_observation[1]['status'],'paused')
    def test_normal_view_never_drops_capture_due_to_submillisecond_clock_jitter(self):
        clock=[100.];reads=[0];seen=[];c=CameraSession(0)
        def read():
            if c.preview_frame:seen.append(c.preview_frame[1])
            reads[0]+=1;clock[0]+=.0999
            if reads[0]==8:c.stop.set()
            return True,np.zeros((4,4,3),np.uint8)
        cap=Mock();cap.isOpened.return_value=True;cap.read.side_effect=read
        with patch.object(cv2,'VideoCapture',return_value=cap),patch('so101_teach.devices.time.monotonic',side_effect=lambda:clock[0]),patch.object(c.stop,'wait',return_value=False):c.run()
        self.assertEqual(len(set(seen)),7);self.assertEqual(reads[0],8)
    def test_paused_server_transmits_only_preview_and_encodes_each_frame_once(self):
        now=time.monotonic();c=CameraSession(0);c.running=True;c.preview_frame=(np.zeros((20,30,3),np.uint8),now)
        c.observation=(c.preview_frame[0],{'selected':'old'},now);c.set_mode(processing_enabled=False,preview_fps=5)
        r=SimpleNamespace(camera=c,generation=1,camera_encode_lock=threading.Lock(),camera_encoded=OrderedDict())
        with patch.object(cv2,'imencode',wraps=cv2.imencode) as encode:
            first=Runtime.camera_frame(r);Runtime.camera_frame(r);same=Runtime.camera_frame(r,after_preview_at=now)
            self.assertEqual(encode.call_count,1)
        self.assertIn('preview_image',first);self.assertNotIn('image',first);self.assertNotIn('detection',first);self.assertNotIn('preview_image',same)
    def test_remote_pause_is_queued_and_uses_five_fps_without_adopting_old_detection(self):
        link=RemoteLink({'host':'pi','user':'robot'});link.camera_rpc=Mock();c=RemoteCameraSession(link)
        c.set_mode(processing_enabled=False,preview_fps=5);link.camera_rpc.assert_not_called()
        encoded=base64.b64encode(cv2.imencode('.jpg',np.zeros((20,30,3),np.uint8))[1]).decode()
        waits=[]
        c.transport=SimpleNamespace(request=lambda:{'generation':0,'running':True,'server_now':10.,'at':10.,'image':encoded,'detection':{'selected':'old'},'preview_at':10.1,'preview_image':encoded,'error':None},close=Mock())
        c.stop.wait=lambda seconds:(waits.append(seconds),c.stop.set())
        with patch('so101_teach.remote_client.time.monotonic',return_value=10.):c.run()
        self.assertEqual(link.camera_rpc.call_args_list[0].args,('camera_start',{'processing_enabled':False,'preview_fps':5}))
        self.assertEqual(waits,[.2]);self.assertIsNone(c.observation);self.assertIsNotNone(c.preview_frame)
    def test_inflight_remote_frame_cannot_restore_detection_after_pause(self):
        link=RemoteLink({'host':'pi','user':'robot'});link.camera_rpc=Mock();c=RemoteCameraSession(link)
        encoded=base64.b64encode(cv2.imencode('.jpg',np.zeros((20,30,3),np.uint8))[1]).decode()
        def request():
            c.set_mode(processing_enabled=False,preview_fps=5);c.stop.set()
            return {'generation':0,'running':True,'server_now':10.,'at':10.,'image':encoded,'detection':{'selected':'old'},'error':None}
        c.transport=SimpleNamespace(request=request,close=Mock());c.run();self.assertIsNone(c.observation)

class PausedTeachingDemandTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def pause(self):
        a=self.app;self.fake_jig_camera();a.toggle_jig_updates();a.follow_jig.set(True);a.commit_target()
        return a,deepcopy(a.episode['steps']),deepcopy(a.new_step_jig_reference('pallet'))
    def test_save_during_preview_and_full_execution_keeps_teaching_basis(self):
        a,steps,held=self.pause();self.fake_jig_camera((260,-90,75))
        for state in ('preview','measurement','plan','motion'):
            a.playing=state=='preview';a.pending_execution={'started':time.monotonic()} if state=='measurement' else None
            a.remote_plan_job=object() if state=='plan' else None;a.detector.freeze(state=='motion')
            self.assertEqual(a.new_step_jig_reference('pallet'),held,state)
        a.playing=False;a.pending_execution=None;a.remote_plan_job=None;a.detector.freeze(False)
    def test_full_execution_measurement_does_not_overwrite_paused_basis(self):
        a,steps,held=self.pause()
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('play',steps);self.fake_jig_camera((260,-90,75));a.check_pending_execution()
            self.assertEqual(run.call_args.args[2]['pallet']['pose'],[260,-90,75]);self.assertEqual(a.new_step_jig_reference('pallet'),held)
        a.stop_preview(quiet=True);self.assertEqual(a.current_jig_reference('pallet'),held)
    def test_multi_jig_preview_only_acquires_missing_jig(self):
        a,steps,held=self.pause();other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera();a.remember_teaching_jigs(a.teaching_pause_results())
        second=deepcopy(steps[0]);second['id']='other-step';second['jig_id']=other['id'];steps.append(second)
        with patch.object(a,'begin_execution') as run:
            a.prepare_execution('preview',steps);self.assertEqual(a.pending_execution['held_references']['pallet'],held)
            self.fake_jig_camera((300,-60,20));a.check_pending_execution();run.assert_called_once()
            self.assertEqual(run.call_args.args[2]['pallet'],held);self.assertEqual(run.call_args.args[2][other['id']]['pose'],[300,-60,20])
        self.assertEqual(a.held_jig_reference('pallet'),held)
    def test_reread_changes_only_requested_jig_and_blocks_new_save_until_done(self):
        a,steps,held=self.pause();other=a.catalog.duplicate('pallet');a.catalog_changed();self.fake_jig_camera();result=deepcopy(a.camera.observation[1]);a.remember_teaching_jigs({'pallet':result,other['id']:result})
        other_before=deepcopy(a.teaching_jig_results[other['id']]);a.request_jig_read();self.assertIsNone(a.new_step_jig_reference('pallet'))
        self.fake_jig_camera((300,-60,20));frame,result,at=a.camera.observation;a.camera.observation=(frame,{'by_jig':{'pallet':result,other['id']:result}},at)
        a.poll_camera_lifecycle();self.assertEqual(a.held_jig_reference('pallet')['pose'],[300,-60,20]);self.assertEqual(a.teaching_jig_results[other['id']],other_before)
    def test_mode_policy_and_photo_use_raw_frame_without_detection(self):
        a,steps,held=self.pause();c=a.camera;c.set_mode=Mock();a.update_camera_mode();c.set_mode.assert_called_with(processing_enabled=False,preview_fps=5)
        a.request_jig_read();c.set_mode.assert_called_with(processing_enabled=True,preview_fps=10);a.stop_preview(quiet=True)
        a.camera_sleeping=False;a.camera_close_requested=False;done=Mock();a.request_camera_photo(done)
        c.set_mode.assert_called_with(processing_enabled=False,preview_fps=10)
        c.observation=None;c.preview_frame=(np.zeros((20,30,3),np.uint8),time.monotonic());a.poll_camera_lifecycle();done.assert_called_once()

from tests import test_remote as remote_fixtures
class CameraDemandRuntimeTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    def test_reconfiguration_preserves_paused_mode_on_replaced_camera(self):
        r=self.runtime;r.start_camera(processing_enabled=False,preview_fps=5);old=r.camera
        self.bundle['profile']['camera']['width']=640;r.configure(self.bundle)
        self.assertIsNot(r.camera,old);self.assertFalse(r.camera.processing_enabled);self.assertEqual(r.camera.preview_fps,5)
    def test_mode_rpc_requires_current_lease_and_changes_no_motor_state(self):
        r=self.runtime;r.start_camera()
        result=r.rpc({'lease':self.lease,'id':'pause-camera','method':'camera_mode','args':{'processing_enabled':False,'preview_fps':5}})
        self.assertTrue(result['ok']);self.assertFalse(r.camera.processing_enabled);self.assertIsNone(r.session)
        with self.assertRaises(ValueError):r.rpc({'lease':'not-owner','id':'resume-camera','method':'camera_mode','args':{'processing_enabled':True,'preview_fps':10}})
        self.assertFalse(r.camera.processing_enabled)
    def test_remote_receiver_applies_pause_and_resume_over_real_http(self):
        from so101_teach.remote_server import Server,Handler
        r=self.runtime;server=Server(('127.0.0.1',0),Handler);server.runtime=r;server.token='test-camera-only'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        link=RemoteLink({'host':'localhost','user':'robot'});link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token;link.lease=self.lease;link.generation=r.generation
        c=RemoteCameraSession(link);c.set_mode(processing_enabled=False,preview_fps=5)
        def wait_for(predicate):
            deadline=time.monotonic()+1
            while not predicate() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(predicate(),c.error)
        try:
            c.start();wait_for(lambda:r.camera is not None and getattr(r.camera,'processing_enabled',None) is False)
            c.set_mode(processing_enabled=True,preview_fps=10);wait_for(lambda:r.camera.processing_enabled)
            c.set_mode(processing_enabled=False,preview_fps=5);wait_for(lambda:not r.camera.processing_enabled)
            self.assertIsNone(r.session);self.assertIsNone(c.error)
        finally:c.close();c.join(2);server.shutdown();server.server_close()
        self.assertFalse(r.camera.running)
