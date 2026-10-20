from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
import base64,os,threading,time,unittest
from unittest.mock import Mock,patch
import cv2,numpy as np
from so101_teach.shared_inference import FrameInference
from so101_teach.devices import CameraSession
from so101_teach.remote_server import Runtime
from tests.test_model_regions import SETTINGS


def evidence(frame):
    rows=[{'class_id':1,'confidence':.9,'box':[0,0,20,20],'mask':np.ones(frame.shape[:2],bool)}]
    shared=FrameInference(frame,SETTINGS,lambda *_:rows);shared.get(frame,SETTINGS)
    return shared.to_wire()


class SharedCameraTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.random.default_rng(4).integers(0,256,(40,60,3),dtype=np.uint8)
        self.packet=evidence(self.frame)
    def test_lossless_wire_roundtrip_reuses_masks_but_jpeg_and_other_models_do_not(self):
        raw=cv2.imdecode(cv2.imencode('.png',self.frame)[1],cv2.IMREAD_COLOR)
        restored=FrameInference.from_wire(raw,SETTINGS,self.packet)
        self.assertIsNotNone(restored);self.assertTrue(restored.get(raw,SETTINGS)[0]['mask'].all())
        jpeg=cv2.imdecode(cv2.imencode('.jpg',self.frame)[1],cv2.IMREAD_COLOR)
        self.assertIsNone(FrameInference.from_wire(jpeg,SETTINGS,self.packet))
        self.assertIsNone(FrameInference.from_wire(raw,{**SETTINGS,'confidence':.6},self.packet))
        self.assertIsNone(FrameInference.from_wire(raw,{**SETTINGS,'model_identity':'b'*64},self.packet))
    def test_malformed_wire_evidence_cannot_become_a_pass(self):
        for change in ({'class_id':True},{'confidence':float('nan')},{'mask':'bad'},{'box':[1,2]}):
            value=deepcopy(self.packet);value['rows'][0].update(change)
            with self.assertRaises(ValueError):FrameInference.from_wire(self.frame,SETTINGS,value)
        value={**self.packet,'rows':self.packet['rows']*51}
        with self.assertRaises(ValueError):FrameInference.from_wire(self.frame,SETTINGS,value)
    def test_server_uses_png_for_shared_capture_and_keeps_new_preview_separate(self):
        now=time.monotonic();camera=CameraSession(0);camera.running=True
        data={'selected':None,'candidates':[],'shared_ai':self.packet}
        camera.observation=(self.frame,data,now-.1);camera.preview_frame=(np.zeros_like(self.frame),now)
        runtime=SimpleNamespace(camera=camera,generation=1,camera_encode_lock=threading.Lock(),camera_encoded=OrderedDict())
        reply=Runtime.camera_frame(runtime)
        raw=base64.b64decode(reply['image']);self.assertTrue(raw.startswith(b'\x89PNG'))
        decoded=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
        self.assertIsNotNone(FrameInference.from_wire(decoded,SETTINGS,reply['detection']['shared_ai']))
        preview=cv2.imdecode(np.frombuffer(base64.b64decode(reply['preview_image']),np.uint8),cv2.IMREAD_COLOR)
        self.assertIsNone(FrameInference.from_wire(preview,SETTINGS,self.packet))
        camera.preview_frame=(self.frame.copy(),now)
        self.assertNotIn('shared_ai',camera.preview_observation[1])
        self.assertIn('shared_ai',camera.observation[1])
        camera.observation=(self.frame,{'selected':None},now-.1)
        self.assertTrue(base64.b64decode(Runtime.camera_frame(runtime)['image']).startswith(b'\xff\xd8'))
    def test_camera_routes_shared_detection_but_never_turns_prepare_into_adoption(self):
        calls=[]
        class Detector:
            def process(self,frame):calls.append('process');return {'kind':'process'}
            def process_shared(self,frame):calls.append('shared');return {'kind':'shared'}
            def prepare(self,frame):calls.append('prepare');return {'kind':'prepare','selected':None}
        d=Detector();camera=CameraSession(0,processor=d.process);camera.set_shared_inference(True)
        thread=threading.Thread(target=camera.process_frames);thread.start()
        try:
            for processor,expected in ((d.process,'shared'),(d.prepare,'prepare')):
                camera.set_processor(processor)
                with camera.frame_lock:camera.pending_frame=(self.frame,time.monotonic());camera.frame_ready.set()
                until=time.monotonic()+2
                while camera.observation is None and time.monotonic()<until:time.sleep(.005)
                self.assertEqual(camera.observation[1]['kind'],expected)
            self.assertEqual(calls,['shared','prepare'])
        finally:camera.stop.set();camera.frame_ready.set();thread.join(2)
    def test_remote_camera_sends_share_enable_and_disable_without_robot_commands(self):
        from so101_teach.remote_client import RemoteLink,RemoteCameraSession
        link=RemoteLink({'host':'pi','user':'robot'});link.camera_rpc=Mock()
        camera=RemoteCameraSession(link);camera.set_shared_inference(True);count=[0]
        def request():
            count[0]+=1
            if count[0]==1:camera.set_shared_inference(False)
            else:camera.stop.set()
            return {'generation':0,'running':True,'error':None,'server_now':time.monotonic()}
        camera.transport=SimpleNamespace(request=request,close=Mock());camera.run()
        self.assertTrue(link.camera_rpc.call_args_list[0].args[1]['share_inference'])
        modes=[call.args[1] for call in link.camera_rpc.call_args_list if call.args[0]=='camera_mode']
        self.assertEqual(modes[-1]['share_inference'],False)
        self.assertTrue(all(call.args[0].startswith('camera_') for call in link.camera_rpc.call_args_list))
    def test_live_detector_supplies_all_classes_once_and_retains_wire_evidence(self):
        from so101_teach.vision_service import MultiDetector
        from tests.test_model_reacquisition import found
        catalog=SimpleNamespace(items={'pallet':{}},revision=0,mesh=lambda _: {'shape':'rectangle'})
        detector=MultiDetector(catalog,{'jig_detection':deepcopy(SETTINGS)})
        provider=Mock();provider.predict.return_value=[{**self.packet['rows'][0],'mask':np.ones(self.frame.shape[:2],bool)}]
        detector.shared_region_model=provider;detector.refresh()
        detector.latches['pallet'].update=lambda raw,*args,**kwargs:raw
        try:
            with patch('so101_teach.vision_service.detect_mask_seeded',return_value=found()):
                result=detector.process_shared(self.frame)
            provider.predict.assert_called_once()
            shared=FrameInference.from_wire(self.frame,SETTINGS,result['shared_ai'])
            self.assertIsNotNone(shared)
            self.assertEqual(detector.finish_observation(result,time.monotonic())['shared_ai'],result['shared_ai'])
            self.assertIsNone(detector.region_model)
        finally:detector.close()


class SharedCameraUITests(unittest.TestCase):
    def test_inspection_view_uses_matching_fresh_capture_and_preserves_age_limit(self):
        from tests.test_camera_overview import CameraOverviewTests
        from so101_teach.domain import ROOT
        from PIL import ImageGrab
        fixture=CameraOverviewTests();fixture.setUp()
        camera=None
        try:
            frame,data=fixture.camera();a=fixture.app;p=a.camera_inspection
            p.worker.close();p.worker=Mock(result=None)
            p.mode.set('카메라 영상');a.camera_tabs.select(p);fixture.root.update()
            camera=CameraSession('unused');camera.running=True;a.camera=camera
            a.update_camera_mode();self.assertTrue(camera.share_inference)
            now=time.monotonic();packet=evidence(frame);data={**data,'shared_ai':packet}
            preview=frame.copy();preview[0,0]^=255
            camera.observation=(frame,data,now-.1);camera.preview_frame=(preview,now)
            with patch.object(p,'update_frame',wraps=p.update_frame) as update:
                a.draw_camera_frame(now);fixture.root.update()
                self.assertIs(update.call_args.args[0],frame)
                self.assertIs(update.call_args.kwargs['inference'],packet)
                image=ImageGrab.grab(xdisplay=os.environ['DISPLAY'])
                output=ROOT/'verification/shared-inference-20261020/ui-shared.png'
                output.parent.mkdir(parents=True,exist_ok=True)
                image.save(output)
                camera.observation=(frame,data,time.monotonic()-1.2)
                camera.preview_frame=(preview,time.monotonic())
                a.draw_camera_frame(time.monotonic())
                self.assertIs(update.call_args.args[0],preview)
        finally:
            if camera:camera.running=False
            fixture.tearDown()
