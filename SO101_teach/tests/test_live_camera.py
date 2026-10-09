import base64,threading,time,unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock,patch
import cv2,numpy as np
from so101_teach.devices import CameraSession
from so101_teach.vision_service import MultiDetector
from so101_teach.remote_server import Runtime
from so101_teach.remote_client import RemoteCameraSession,RemoteLink

class LiveCameraTests(unittest.TestCase):
    def test_capture_continues_while_detection_blocks_and_only_latest_waits(self):
        entered=threading.Event();release=threading.Event();captured=threading.Event();unblock_capture=threading.Event();processed=threading.Event();calls=[];reads=[0]
        def processor(frame):
            calls.append(int(frame[0,0,0]))
            if len(calls)==1:entered.set();release.wait(2)
            else:processed.set()
            return {'number':int(frame[0,0,0])}
        def read():
            reads[0]+=1
            if reads[0]==5:captured.set();unblock_capture.wait(2)
            return True,np.full((20,30,3),reads[0],np.uint8)
        cap=Mock();cap.isOpened.return_value=True;cap.read.side_effect=read;c=CameraSession(0,processor=processor);c.max_fps=100;c.preview_fps=100
        with patch.object(cv2,'VideoCapture',return_value=cap):
            c.start()
            try:
                self.assertTrue(entered.wait(1));self.assertTrue(captured.wait(1))
                self.assertEqual(int(c.preview_observation[0][0,0,0]),4);self.assertIsNone(c.observation)
                capture_at=c.preview_observation[2];release.set();self.assertTrue(processed.wait(1))
                deadline=time.monotonic()+1
                while c.observation is None or c.observation[1]['number']!=4:
                    if time.monotonic()>deadline:self.fail('latest frame was not processed')
                    time.sleep(.005)
                self.assertEqual(calls[:2],[1,4]);self.assertEqual(c.observation[2],capture_at)
                self.assertEqual(int(c.observation[0][0,0,0]),c.observation[1]['number'])
            finally:c.close();release.set();unblock_capture.set();self.assertTrue(c.join(2))
        cap.release.assert_called_once()
    def test_live_detection_disappears_while_measurement_hold_remains(self):
        from tests.test_pose_hold import result
        catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda k:{'sha256':'a'*64});d=MultiDetector(catalog,{})
        missing={'selected':None,'candidates':[],'status':'not_found','verified_for_motion':False}
        with patch('so101_teach.vision_service.detect',side_effect=[result(100,strong=True)]*5+[missing]), patch('so101_teach.vision_service.time.monotonic') as clock:
            for at in (10.,10.8,11.6,12.4,13.1,13.2):
                clock.return_value=at;out=d.process(np.zeros((48,64,3),np.uint8))
        self.assertIsNotNone(out['by_jig']['pallet']['selected']);self.assertTrue(out['by_jig']['pallet']['pose_held'])
        self.assertIsNone(out['live_by_jig']['pallet']['selected']);self.assertFalse(out['live_by_jig']['pallet']['pose_held'])
    def test_preview_drops_stale_detection_without_hiding_video(self):
        c=CameraSession(0);c.observation=(np.zeros((20,30,3),np.uint8),{'selected':{'old':True},'candidates':[]},10.)
        c.preview_frame=(np.ones((20,30,3),np.uint8),11.6)
        self.assertEqual(int(c.preview_observation[0][0,0,0]),1);self.assertIsNone(c.preview_observation[1]['selected'])
        self.assertIsNotNone(c.observation[1]['selected'])
    def test_server_keeps_capture_measurement_pair_and_deduplicates_images(self):
        now=time.monotonic();c=CameraSession(0);c.running=True;c.observation=(np.full((20,30,3),30,np.uint8),{'selected':None,'candidates':[]},now-.2);c.preview_frame=(np.full((20,30,3),200,np.uint8),now)
        runtime=SimpleNamespace(camera=c,generation=1,camera_encode_lock=threading.Lock(),camera_encoded=OrderedDict())
        data=Runtime.camera_frame(runtime)
        decode=lambda s:cv2.imdecode(np.frombuffer(base64.b64decode(s),np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(int(decode(data['image'])[0,0,0]),30);self.assertEqual(int(decode(data['preview_image'])[0,0,0]),200)
        same=Runtime.camera_frame(runtime,after_at=now-.2,after_preview_at=now)
        self.assertNotIn('image',same);self.assertNotIn('preview_image',same);self.assertEqual(same['at'],now-.2)
    def test_remote_preview_can_arrive_without_new_measurement(self):
        def encoded(value):return base64.b64encode(cv2.imencode('.jpg',np.full((20,30,3),value,np.uint8))[1]).decode()
        link=RemoteLink({'host':'pi','user':'robot'});link.camera_rpc=Mock();c=RemoteCameraSession(link);counter=[0]
        first={'generation':0,'running':True,'error':None,'server_now':10.,'at':10.,'image':encoded(30),'detection':{'selected':None,'candidates':[]},'preview_at':10.}
        def request():
            counter[0]+=1
            if counter[0]==1:return first
            c.stop.set();return {**{k:v for k,v in first.items() if k!='image'},'preview_at':10.1,'preview_image':encoded(200),'server_now':10.1}
        c.transport=SimpleNamespace(request=request,close=Mock());c.run()
        self.assertEqual(int(c.observation[0][0,0,0]),30);self.assertEqual(int(c.frame[0,0,0]),200)
    def test_rebinding_detector_discards_inflight_old_configuration(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        def old(frame):entered.set();release.wait(1);finished.set();return {'old':True}
        c=CameraSession(0,processor=old);c.pending_frame=(np.zeros((5,5,3),np.uint8),10.);c.frame_ready.set()
        thread=threading.Thread(target=c.process_frames);thread.start()
        try:
            self.assertTrue(entered.wait(1));c.set_processor(lambda frame:{'new':True});release.set();self.assertTrue(finished.wait(1))
            time.sleep(.03);self.assertIsNone(c.observation)
            with c.frame_lock:c.pending_frame=(np.ones((5,5,3),np.uint8),11.);c.frame_ready.set()
            deadline=time.monotonic()+1
            while c.observation is None and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(c.observation[1],{'new':True});self.assertEqual(c.observation[2],11.)
        finally:c.close();release.set();thread.join(1)

from tests import test_remote as remote_fixtures
class LiveCameraRuntimeTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    def test_roi_change_reuses_capture_and_rebinds_detection(self):
        class RebindCamera(remote_fixtures.FakeCamera):
            def __init__(self,**kw):super().__init__(**kw);self.processor=kw['processor'];self.closed=False
            def set_processor(self,processor):self.processor=processor;self.observation=None
            def close(self):self.closed=True;super().close()
        r=self.runtime;r.camera_factory=RebindCamera;r.start_camera();camera=r.camera
        self.bundle['jigs']['pallet']['roi']=[[.1,.2],[.7,.1],[.8,.8],[.2,.9]]
        with patch.object(camera,'set_processor',wraps=camera.set_processor) as rebind:r.configure(self.bundle)
        self.assertIs(r.camera,camera);self.assertFalse(camera.closed);self.assertTrue(camera.running);rebind.assert_called_once()
        self.assertIs(camera.processor.__self__,r.detector)
        self.assertEqual(r.catalog.items['pallet']['roi'],self.bundle['jigs']['pallet']['roi'])
    def test_configuration_transition_keeps_video_channel_alive_without_stale_measurement(self):
        now=time.monotonic();c=CameraSession(0);c.preview_frame=(np.zeros((20,30,3),np.uint8),now);c.observation=(np.zeros((20,30,3),np.uint8),{},now)
        self.runtime.camera=c;self.runtime.camera_reconfiguring=True
        out=self.runtime.camera_frame();self.assertTrue(out['running']);self.assertNotIn('detection',out);self.assertIn('preview_image',out)
