"""Episode-scoped camera/model preparation without robot or camera hardware."""
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.model_regions import RegionModel
from so101_teach.devices import CameraSession
from tests import test_episode_cli as fixture,test_remote as remote_fixture
from tests.test_model_regions import SETTINGS

cli=fixture.cli


class PreparedProvider(RegionModel):
    def __init__(self):
        super().__init__();self.calls=0;self.starts=0;self.closed=0
    def predict(self,frame,settings):
        if not self.warmed or self.key!=(settings['model'],settings['model_identity']) or self.process.poll() is not None:
            self.starts+=1;self.process=Mock();self.process.poll.return_value=None
        self.calls+=1;self.key=(settings['model'],settings['model_identity']);self.warmed=True
        return []
    def close(self):
        self.closed+=1;self.process=None;self.key=None;self.warmed=False


class PreparationTests(unittest.TestCase):
    def test_warmup_reuses_only_a_live_matching_worker(self):
        provider=PreparedProvider();frame=np.zeros((24,32,3),np.uint8)
        self.assertTrue(provider.prepare(frame,SETTINGS))
        worker=provider.process
        self.assertFalse(provider.prepare(frame,SETTINGS));self.assertIs(provider.process,worker)
        self.assertEqual(provider.calls,1)
        worker.poll.return_value=1
        self.assertTrue(provider.prepare(frame,SETTINGS));self.assertEqual(provider.starts,2)
        self.assertTrue(provider.prepare(frame,{**SETTINGS,'model_identity':'b'*64}))
        self.assertEqual(provider.starts,3)
        provider.close();self.assertTrue(provider.prepare(frame,SETTINGS));self.assertEqual(provider.starts,4)
        provider.close()

    def test_preparation_never_fills_pose_samples_or_tracking(self):
        from types import SimpleNamespace
        from so101_teach.vision_service import MultiDetector
        catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda key:{'shape':'ring'})
        detector=MultiDetector(catalog,{'jig_detection':SETTINGS});provider=PreparedProvider();detector.region_model=provider
        detector.refresh();detector.clear();started=detector.latches['pallet'].stability.started
        frame=np.zeros((48,64,3),np.uint8)
        for _ in range(3):
            out=detector.prepare(frame)
            self.assertTrue(out['vision_ready']);self.assertIsNone(out['selected']);self.assertEqual(out['candidates'],[])
        self.assertEqual(provider.calls,1)
        self.assertEqual(detector.latches['pallet'].stability.samples,[])
        self.assertEqual(detector.latches['pallet'].stability.started,started)
        self.assertEqual(detector.region_tracks,{})
        # A new measurement must call AI again; the preparation mask is absent.
        with patch('so101_teach.vision_service.detect_mask_seeded',return_value={'status':'model_not_found','selected':None,'candidates':[]}):
            detector.process(frame.copy())
        self.assertEqual(provider.calls,2);detector.close()


class RuntimePreparationTests(unittest.TestCase):
    setUp=remote_fixture.RemoteTests.setUp
    tearDown=remote_fixture.RemoteTests.tearDown
    def wait_for(self,predicate):
        end=time.monotonic()+2
        while not predicate():
            if time.monotonic()>end:self.fail('camera preparation did not finish')
            time.sleep(.005)

    def test_real_camera_lifecycle_keeps_worker_and_discards_preparation_frame(self):
        r=self.runtime;r.camera_factory=CameraSession;r.profile['jig_detection']=deepcopy(SETTINGS)
        provider=PreparedProvider();r.detector.region_model=provider
        cap=Mock();cap.isOpened.return_value=True;cap.read.return_value=(True,np.zeros((48,64,3),np.uint8))
        with patch('cv2.VideoCapture',return_value=cap):
            r.call('camera_prepare',{})
            camera=r.camera
            self.wait_for(lambda:camera.observation is not None)
            self.assertTrue(camera.observation[1]['vision_ready'])
            worker=provider.process;self.assertEqual(provider.calls,1)
            r.call('camera_mode',{'processing_enabled':False,'preview_fps':10})
            self.assertTrue(camera.running);self.assertIs(provider.process,worker)
            r.call('camera_prepare',{})
            self.wait_for(lambda:camera.observation is not None)
            self.assertEqual(provider.calls,1)
            r.call('detect_clear',{});started=time.monotonic()
            with patch('so101_teach.vision_service.detect_mask_seeded',return_value={'status':'model_not_found','selected':None,'candidates':[]}):
                r.call('camera_start',{'processing_enabled':True,'preview_fps':10})
                self.assertIsNone(camera.observation)
                self.wait_for(lambda:camera.observation is not None)
                self.assertGreaterEqual(camera.observation[2],started)
                self.assertNotIn('vision_ready',camera.observation[1])
                self.assertGreaterEqual(provider.calls,2);self.assertIs(provider.process,worker)
                r.call('camera_stop',{});self.assertTrue(camera.join(2))
            self.assertEqual(provider.closed,1);self.assertIsNone(provider.process)
        cap.release.assert_called_once()


class EpisodePreparationTests(unittest.TestCase):
    def setUp(self):
        self.now=100.;self.events=[];self.control=cli.Control();outer=self
        class Link(fixture.FakeLink):
            def __init__(self):
                super().__init__();self.ready_at=None;self.started=None;self.times=[];self.prepare_error=None
            def rpc(self,method,args=None):
                self.times.append((method,outer.now))
                if method=='camera_prepare':self.ready_at=outer.now+2 if self.ready_at is None else outer.now
                if method=='connect':outer.now+=1  # Motor connection overlaps the camera/model startup.
                if method=='detect_clear':self.started=outer.now
                return super().rpc(method,args)
            def post(self,endpoint,body=None):
                if endpoint!='/camera':return super().post(endpoint,body)
                if self.preparing:
                    if self.prepare_error:return {'detection':{'error':self.prepare_error}}
                    return {'at':outer.now,'detection':{'vision_ready':outer.now>=self.ready_at}}
                ready=self.started is not None and outer.now>=self.started+4
                current={'j':{'selected':{'metric':{'center_xy_mm':[1,2],'yaw_deg':3}},'pose_measured_at':outer.now,'acquisition_completed_attempts':1}} if ready else {}
                return {'at':outer.now,'detection':{'by_jig':current}}
        self.link=Link();self.runner=cli.Runner(self.link,self.events.append,self.control,100,linear=fixture.FakeLinear())
        self.episode={'steps':[{'id':'work','name':'work','jig_id':'j','ticks':{}}]}
        self.bundle={'acquisition_seconds':4.,'acquisition_attempts':1,'hold_seconds':5.,'mesh_hashes':{'j':'abc'}}
        self.addCleanup(patch.stopall)
        patch.object(cli.time,'monotonic',side_effect=lambda:self.now).start()
        patch.object(cli.time,'sleep',side_effect=self.sleep).start()
        self.runner.deadline=200.
    def sleep(self,seconds):self.now+=seconds

    def test_preparation_overlaps_connect_and_does_not_consume_measurement_window(self):
        self.runner.execute(self.episode,self.bundle,400,100.)
        methods=[m for m,_ in self.link.calls]
        self.assertLess(methods.index('camera_prepare'),methods.index('connect'))
        self.assertGreaterEqual(self.link.started,102.)
        self.assertGreaterEqual(self.now,self.link.started+4.)
        self.assertLess(self.now,106.2)
        self.assertLess(self.events.index('VISION_READY'),self.events.index('JIG_DETECTION_STARTED'))
        self.assertEqual(methods.count('camera_stop'),1)
        self.assertIn(('camera_mode',{'processing_enabled':False,'preview_fps':10}),self.link.calls)
        self.assertFalse(self.runner.camera_owned)

    def test_model_failure_closes_camera_before_any_arm_motion(self):
        self.link.prepare_error='model startup failed'
        with self.assertRaisesRegex(RuntimeError,'카메라·모델 준비 실패'):
            self.runner.execute(self.episode,self.bundle,400,100.)
        self.assertEqual(self.link.calls[-1][0],'camera_stop')
        self.assertFalse(any(m=='command' and a.get('action') in ('move','play','follow') for m,a in self.link.calls))
        self.assertIsNone(self.link.started)

    def test_shutdown_while_preparing_closes_camera(self):
        def stop(seconds):self.now+=seconds;self.control.shutdown.set()
        with patch.object(cli.time,'sleep',side_effect=stop),self.assertRaises(cli.ShutdownRequested):
            self.runner.execute(self.episode,self.bundle,400,100.)
        self.assertEqual(self.link.calls[-1][0],'camera_stop')
        self.assertFalse(any(m=='command' and a.get('action') in ('move','play','follow') for m,a in self.link.calls))

    def test_stale_ready_result_is_not_readiness_for_this_episode(self):
        self.runner.prepare_camera();asked=[]
        def reply(endpoint,body):
            asked.append(self.now)
            return {'at':99. if len(asked)==1 else self.now,'detection':{'vision_ready':True}}
        with patch.object(self.link,'post',side_effect=reply):self.runner.await_camera_ready()
        self.assertEqual(len(asked),2)
        self.assertGreater(self.now,100.)

    def test_preparation_timeout_never_opens_the_measurement_window(self):
        self.link.ready_at=1000.
        with patch.object(self.link,'post',return_value={'at':100.,'detection':{'vision_ready':False}}):
            with self.assertRaisesRegex(RuntimeError,'준비 시간 초과'):
                self.runner.execute(self.episode,self.bundle,400,100.)
        self.assertIsNone(self.link.started)
        self.assertEqual(self.link.calls[-1][0],'camera_stop')

    def test_cleanup_error_preserves_original_preparation_failure(self):
        original=self.link.rpc
        def fail_stop(method,args=None):
            if method=='camera_stop':raise RuntimeError('stop failed')
            return original(method,args)
        self.link.prepare_error='original failure'
        with patch.object(self.link,'rpc',side_effect=fail_stop),patch.object(cli.sys,'stderr'):
            with self.assertRaisesRegex(RuntimeError,'original failure'):
                self.runner.execute(self.episode,self.bundle,400,100.)

    def test_motion_failure_requests_hold_before_camera_cleanup(self):
        def fail_motion(*args):
            self.runner.failure_context['phase']='motion';self.link.mode='MOVING'
            raise RuntimeError('motion failed')
        with patch.object(self.runner,'move',side_effect=fail_motion),self.assertRaisesRegex(RuntimeError,'motion failed'):
            self.runner.execute(self.episode,self.bundle,400,100.)
        hold=next(i for i,(m,a) in enumerate(self.link.calls) if m=='command' and a.get('action')=='hold')
        stop=next(i for i,(m,_) in enumerate(self.link.calls) if m=='camera_stop')
        self.assertLess(hold,stop);self.assertEqual(self.link.mode,'HOLD')

    def test_camera_handoff_waits_for_capture_and_model_shutdown(self):
        self.runner.camera_owned=True;states=iter([{'running':True},{'running':True},{'running':False}])
        with patch.object(self.link,'post',side_effect=lambda *args:next(states)) as poll:
            self.runner.close_episode_camera()
        self.assertEqual(poll.call_count,3);self.assertAlmostEqual(self.now,100.04)
        self.assertFalse(self.runner.camera_owned)
