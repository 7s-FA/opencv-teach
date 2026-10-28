"""A new capture gets new votes; repeated starts and frozen poses retain state."""
import time
import unittest
from tests import test_remote as fixture
from tests.test_cad_three_frame_gate import cad_observation


class Camera(fixture.FakeCamera):
    def __init__(self,**kw):
        super().__init__(**kw);self.processor=kw['processor']
    def set_processor(self,processor):self.processor=processor;self.observation=None


class CameraReconnectTests(unittest.TestCase):
    setUp=fixture.RemoteTests.setUp
    tearDown=fixture.RemoteTests.tearDown

    def exhaust(self):
        latch=self.runtime.detector.latches['pallet']
        latch.clear(time.monotonic()-100)
        self.assertEqual(latch.poll(time.monotonic())['status'],'acquisition_failed')
        self.assertTrue(latch.stability.exhausted)
        return latch

    def adopt_new(self,latch):
        # Opening a device/model can be slow: the first image starts the budget.
        started=time.monotonic()+100
        for i in range(3):
            out=latch.update(cad_observation('new-'+str(i)),started+i*.5)
            if i<2:self.assertIsNone(out['selected'])
        self.assertIsNotNone(out['selected'])
        self.assertEqual(out['adoption_method'],'cad_three_frame_gate')
        self.assertEqual(out['stable_candidate_samples'],3)
        self.assertEqual(out['acquisition_completed_attempts'],1)

    def test_stopped_camera_reconnect_recovers_exhausted_unchanged_configuration(self):
        r=self.runtime;r.start_camera();old=r.camera;r.call('camera_stop',{})
        latch=self.exhaust();detector=r.detector
        r.configure(self.bundle);self.assertIs(r.detector,detector)
        r.start_camera();self.assertIsNot(r.camera,old)
        self.adopt_new(latch)

    def test_first_start_recovers_expired_window_and_discards_old_votes(self):
        latch=self.exhaust();self.runtime.detector.region_tracks['pallet']={'old':True}
        self.runtime.start_camera()
        self.assertEqual(self.runtime.detector.region_tracks,{})
        self.adopt_new(latch)

    def test_preparation_to_measurement_recovers_without_reopening_camera(self):
        r=self.runtime;r.camera_factory=Camera;r.start_camera(prepare=True);camera=r.camera
        latch=self.exhaust();r.start_camera()
        self.assertIs(r.camera,camera);self.assertEqual(camera.processor,r.detector.process)
        self.adopt_new(latch)

    def test_repeated_start_preserves_current_votes(self):
        r=self.runtime;r.start_camera();latch=r.detector.latches['pallet'];at=time.monotonic()
        latch.update(cad_observation('one'),at);generation=r.detector.generation
        r.start_camera();self.assertEqual(r.detector.generation,generation)
        self.assertIsNone(latch.update(cad_observation('two'),at+.5)['selected'])
        self.assertIsNotNone(latch.update(cad_observation('three'),at+1)['selected'])

    def test_frozen_execution_pose_survives_reconnect(self):
        r=self.runtime;r.start_camera();latch=r.detector.latches['pallet'];at=time.monotonic()
        for i in range(3):out=latch.update(cad_observation(str(i)),at+i*.5)
        self.assertIsNotNone(out['selected']);r.detector.freeze(True)
        r.call('camera_stop',{});r.start_camera()
        self.assertEqual(latch.poll(at+100)['selected'],out['selected'])
