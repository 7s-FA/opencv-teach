"""Job/frame binding and failure behavior of the actuator-free PC compute path."""
from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch,Mock
import numpy as np
from so101_teach.pc_compute import ComputeClient,dumps,loads,decode,frame_id,RemoteInspector,start_guard
from so101_teach.pc_compute_service import ComputeServer,ComputeEngine

class Engine:
    def __init__(self):self.calls=[];self.wait=None
    def execute(self,method,frame,args):
        self.calls.append((method,frame,args))
        if self.wait:self.wait.wait(1)
        if method=='fail':raise ValueError('GPU unavailable')
        return {'array':frame,'args':args}

class ComputeTransportTests(unittest.TestCase):
    def setUp(self):
        self.engine=Engine();self.server=ComputeServer(self.engine,'a'*32,'b'*64)
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.frame=np.arange(30*40*3,dtype=np.uint8).reshape(30,40,3);self.client=ComputeClient(self.server.descriptor())
    def test_lossless_pixels_mesh_arrays_and_bound_response(self):
        mesh=np.arange(36,dtype=float).reshape(4,3,3)/17
        value=self.client.call('echo',self.frame,mesh=mesh,options={'roi':[1,2,3,4]})
        np.testing.assert_array_equal(value['array'],self.frame);np.testing.assert_array_equal(value['args']['mesh'],mesh)
        self.assertEqual(self.client.metrics[-1]['frame_id'],frame_id(self.frame))
        self.assertGreater(self.client.metrics[-1]['request_bytes'],0)
    def test_wrong_token_never_executes_or_falls_back(self):
        client=ComputeClient({**self.server.descriptor(),'token':'c'*64})
        with self.assertRaisesRegex(RuntimeError,'403'):client.call('echo',self.frame)
        with self.assertRaisesRegex(RuntimeError,'자동'):client.call('echo',self.frame)
        self.assertFalse(self.engine.calls)
    def test_request_with_another_job_or_frame_never_executes(self):
        d=self.server.descriptor()
        for changes in ({'job_id':'c'*32},{'frame_id':'0'*64},{'contract':'1'*64}):
            value={'request_id':'d'*32,'criteria_id':hashlib.sha256(dumps({})).hexdigest(),'job_id':d['job_id'],'frame_id':frame_id(self.frame),'contract':d['contract'],'method':'echo','args':{},'frame':self.frame,**changes}
            connection=http.client.HTTPConnection(d['host'],d['port'],timeout=2)
            connection.request('POST','/compute',dumps(value),{'Authorization':'Bearer '+d['token']})
            response=connection.getresponse();self.assertEqual(response.status,400);response.read();connection.close()
        self.assertFalse(self.engine.calls)
    def test_reply_for_different_capture_is_rejected(self):
        import so101_teach.pc_compute as module
        original=module.loads
        def altered(raw):
            result=original(raw)
            if 'value' in result:result['frame_id']='0'*64
            return result
        with patch.object(module,'loads',side_effect=altered):
            with self.assertRaisesRegex(ValueError,'불일치'):self.client.call('echo',self.frame)
        self.assertTrue(self.client.failed)
    def test_gpu_error_cannot_be_consumed_as_a_result(self):
        with self.assertRaisesRegex(RuntimeError,'GPU unavailable'):self.client.call('fail',self.frame)
        self.assertTrue(self.client.failed)
    def test_timeout_and_cancel_discard_delayed_results(self):
        self.engine.wait=threading.Event()
        with self.assertRaises(TimeoutError):self.client.call('echo',self.frame,timeout=.05)
        self.engine.wait.set();self.assertTrue(self.client.failed)
        stop=threading.Event();stop.set();client=ComputeClient(self.server.descriptor(),stop=stop)
        with self.assertRaisesRegex(RuntimeError,'취소'):client.call('echo',self.frame)
    def test_health_remains_available_while_geometry_is_busy(self):
        self.engine.wait=threading.Event();finished=[]
        worker=threading.Thread(target=lambda:finished.append(self.client.call('echo',self.frame)));worker.start()
        until=time.monotonic()+1
        while not self.engine.calls and time.monotonic()<until:time.sleep(.005)
        probe=ComputeClient(self.server.descriptor());self.assertTrue(probe.call('hello',timeout=.2)['ready'])
        self.engine.wait.set();worker.join(1);self.assertEqual(len(finished),1)
    def test_inspection_cancel_after_rpc_does_not_return_pass(self):
        stop=threading.Event();client=Mock()
        def delayed(*args,**kwargs):stop.set();return [{'quality':'normal'}]
        client.call.side_effect=delayed;inspector=RemoteInspector(client);inspector.stop=stop
        self.assertEqual(inspector.inspect(self.frame,[],{}),[])
    def test_unsupported_model_is_rejected_before_device_preparation(self):
        from so101_teach.pc_compute import validate_profile
        n={'method':'model_roi','model':'n','model_identity':'a'*64,'confidence':.5}
        validate_profile({'jig_detection':n,'part_inspection':n})
        for key in ('jig_detection','part_inspection'):
            with self.assertRaisesRegex(ValueError,'YOLO26n'):
                validate_profile({'jig_detection':n,'part_inspection':n,key:{**n,'model':'s'}})

    def test_codec_rejects_nonfinite_overbudget_and_bombs(self):
        for value in (float('nan'),np.array([float('inf')])):
            with self.assertRaises(ValueError):dumps(value)
        wire=json.loads(dumps(np.arange(100,dtype=np.uint8)));wire['shape']=[1]
        with self.assertRaises(ValueError):decode(wire)
        wire['shape']=[2**40]
        with self.assertRaises(ValueError):decode(wire)
        wire['shape']=[100];wire['dtype']='O'
        with self.assertRaises(ValueError):decode(wire)
    def test_disconnect_guard_calls_failure_once(self):
        done=threading.Event();failed=threading.Event();reasons=[]
        with patch.object(ComputeClient,'call',side_effect=ConnectionError('offline')):
            thread=start_guard(self.client,done,lambda value:(reasons.append(value),failed.set()))
            self.assertTrue(failed.wait(1));done.set();thread.join(1)
        self.assertEqual(reasons,['offline'])

class ComputeGeometryTests(unittest.TestCase):
    def test_transport_preserves_actual_jig_geometry(self):
        import cv2
        from so101_teach.domain import ROOT
        from so101_teach.mask_pose import detect_mask_seeded
        folder=ROOT/'tests/fixtures/cad-pallet-current';fixture=json.loads((folder/'fixture.json').read_text())
        case=fixture['cases']['B051']['jigs']['pallet'];frame=cv2.imread(str(folder/'B051.png'))
        with np.load(folder/'B051.npz') as values:
            rows=[{'class_id':int(k),'confidence':float(s),'box':b.tolist(),'mask':m} for k,s,b,m in zip(values['classes'],values['scores'],values['boxes'],values['masks'])]
        args={'mesh':case['mesh'],'roi':case['roi'],'profile':case['profile'],'detections':rows,'settings':fixture['settings']}
        expected=detect_mask_seeded(frame,**deepcopy(args))
        engine=ComputeEngine(Mock());server=ComputeServer(engine,'a'*32,'b'*64)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:actual=ComputeClient(server.descriptor()).call('mask_pose',frame,timeout=10,**args)
        finally:server.shutdown();server.server_close();engine.close()
        self.assertEqual(actual,expected)
