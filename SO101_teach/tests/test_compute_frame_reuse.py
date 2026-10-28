import http.client,hashlib,json,threading,unittest
import numpy as np
from tests.test_pc_compute import Engine
from so101_teach.pc_compute import ComputeClient,dumps,frame_id
from so101_teach.pc_compute_service import ComputeServer,ComputeEngine
from unittest.mock import Mock,patch

class FrameReuseTests(unittest.TestCase):
    def setUp(self):
        self.engine=Engine();self.server=ComputeServer(self.engine,'a'*32,'b'*64)
        threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.client=ComputeClient(self.server.descriptor());self.addCleanup(self.client.close)
        self.frame=np.random.default_rng(3).integers(0,256,(120,160,3),dtype=np.uint8)
    def test_same_pixels_reuse_connection_and_uploaded_frame(self):
        first=self.client.call('echo',self.frame);connection=self.client.connection
        second=self.client.call('echo',self.frame.copy())
        np.testing.assert_array_equal(first['array'],second['array'])
        self.assertIs(connection,self.client.connection);self.assertTrue(self.client.metrics[-1]['frame_reused'])
        self.assertLess(self.client.metrics[-1]['request_bytes'],self.client.metrics[-2]['request_bytes']/10)
        self.assertIs(self.engine.calls[0][1],self.engine.calls[1][1])
    def test_inplace_pixel_mutation_uploads_new_snapshot(self):
        expected=self.frame.copy();self.client.call('echo',self.frame)
        self.frame[0,0]+=1;actual=self.client.call('echo',self.frame)
        np.testing.assert_array_equal(actual['array'],self.frame);np.testing.assert_array_equal(self.engine.calls[0][1],expected)
        self.assertFalse(self.client.metrics[-1]['frame_reused'])
    def test_new_capture_with_same_pixels_still_executes_new_prediction(self):
        self.client.call('predict',self.frame)
        self.client.call('predict',self.frame.copy())
        self.assertEqual([row[0] for row in self.engine.calls],['predict','predict'])
        self.assertTrue(self.client.metrics[-1]['frame_reused'])
        self.assertNotEqual(self.client.metrics[0]['request_id'],self.client.metrics[1]['request_id'])
    def test_a_waiter_failure_also_invalidates_the_inflight_reply(self):
        import time
        self.engine.wait=threading.Event();errors=[];values=[]
        def call():
            try:values.append(self.client.call('echo',self.frame))
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=call);thread.start()
        try:
            until=time.monotonic()+1
            while not self.engine.calls and time.monotonic()<until:time.sleep(.002)
            self.assertTrue(self.engine.calls)
            with self.assertRaises(TimeoutError):self.client.call('echo',self.frame,timeout=.01)
        finally:self.engine.wait.set();thread.join(2)
        self.assertFalse(values);self.assertEqual(len(errors),1);self.assertIsInstance(errors[0],RuntimeError)
    def test_idle_and_explicit_close_drop_connection_local_reference(self):
        self.client.call('echo',self.frame);connection=self.client.connection;self.client.last_call-=3
        self.client.call('echo',self.frame);self.assertIsNot(connection,self.client.connection);self.assertFalse(self.client.metrics[-1]['frame_reused'])
        self.client.close();self.client.call('echo',self.frame);self.assertFalse(self.client.metrics[-1]['frame_reused'])
    def test_retained_frame_still_counts_against_the_total_array_budget(self):
        import so101_teach.pc_compute_service as service
        frame=np.zeros((30,20,3),np.uint8)
        with patch.object(service,'MAX_BYTES',2048):
            self.client.call('echo',frame)
            with self.assertRaisesRegex(RuntimeError,'400'):self.client.call('echo',frame,mask=np.zeros(300,np.uint8))
        self.assertEqual(len(self.engine.calls),1)
    def test_reference_from_another_connection_is_rejected_without_compute(self):
        self.client.call('echo',self.frame);d=self.server.descriptor()
        value={'request_id':'d'*32,'criteria_id':hashlib.sha256(dumps({})).hexdigest(),'job_id':d['job_id'],'frame_id':frame_id(self.frame),'contract':d['contract'],'method':'echo','args':{},'frame':None,'frame_reference':True}
        c=http.client.HTTPConnection(d['host'],d['port'],timeout=2)
        try:
            c.request('POST','/compute',dumps(value),{'Authorization':'Bearer '+d['token']})
            r=c.getresponse();self.assertEqual(r.status,400);r.read()
        finally:c.close()
        self.assertEqual(len(self.engine.calls),1)
    def test_concurrent_calls_do_not_pair_another_frame_with_arguments(self):
        errors=[]
        def call(n):
            try:
                frame=np.full_like(self.frame,n);out=self.client.call('echo',frame,n=n)
                self.assertTrue((out['array']==out['args']['n']).all())
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=call,args=(n,)) for n in range(6)]
        for t in threads:t.start()
        for t in threads:t.join(3)
        self.assertFalse(errors);self.assertFalse(any(t.is_alive() for t in threads))

class GeometryReuseTests(unittest.TestCase):
    def test_repeated_requests_preserve_actual_geometry_and_share_exact_frame(self):
        import cv2
        from so101_teach.domain import ROOT
        from so101_teach.mask_pose import detect_mask_seeded
        folder=ROOT/'tests/fixtures/cad-pallet-current';fixture=json.loads((folder/'fixture.json').read_text())
        case=fixture['cases']['B051']['jigs']['pallet'];frame=cv2.imread(str(folder/'B051.png'))
        with np.load(folder/'B051.npz') as values:
            rows=[{'class_id':int(k),'confidence':float(s),'box':b.tolist(),'mask':m} for k,s,b,m in zip(values['classes'],values['scores'],values['boxes'],values['masks'])]
        job={k:case[k] for k in ('mesh','roi','profile')}
        expected=detect_mask_seeded(frame,**job,detections=rows,settings=fixture['settings'])
        engine=ComputeEngine(Mock());server=ComputeServer(engine,'a'*32,'b'*64)
        threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True).start();client=ComputeClient(server.descriptor())
        try:
            result={key:client.mask_pose(frame,**job,detections=rows,settings=fixture['settings']) for key in ('first','second')}
            self.assertEqual(result,{'first':expected,'second':expected})
            prepared=engine.prepared;self.assertEqual(client.mask_pose(frame,**job,detections=rows,settings=fixture['settings']),expected)
            self.assertIs(prepared,engine.prepared)
        finally:client.close();server.shutdown();server.server_close();engine.close()
