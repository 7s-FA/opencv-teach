"""Lossless, bounded diagnostics on device-free production processing paths."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import cv2
import numpy as np

from so101_teach.decision_evidence import DecisionEvidence, start


class EvidenceFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.recorders=[]
        self.addCleanup(lambda:[r.close() for r in self.recorders])
    def recorder(self,**kw):
        r=DecisionEvidence(self.temp.name,{'run_id':'a'*32,'arm':'arm2'},**kw)
        self.recorders.append(r);return r
    def document(self,r,ref):return json.loads((r.folder/ref['frame_id']/'frame.json').read_text())


class EvidenceTests(EvidenceFixture,unittest.TestCase):
    def test_exact_pixels_masks_and_capture_time_survive_input_mutation(self):
        r=self.recorder();im=np.arange(12*16*3,dtype=np.uint8).reshape(12,16,3);original=im.copy();mask=im[:,:,0]>80;expected=mask.copy()
        ref=r.submit(im,12.345,masks=[{'class_id':0,'confidence':.8,'box':[0,0,16,12],'mask':mask}],search_masks={'carrier':mask.astype(np.uint8)*255},metadata={'reason':'boundary','frame_number':17})
        im[:]=0;mask[:]=False;result=r.close();self.assertTrue(result['complete'])
        doc=self.document(r,ref);saved=cv2.imread(str(r.folder/ref['frame_id']/'frame.png'))
        np.testing.assert_array_equal(saved,original)
        with np.load(r.folder/ref['frame_id']/'masks.npz',allow_pickle=False) as z:np.testing.assert_array_equal(z['ai_000'],expected)
        self.assertEqual(doc['captured_at'],12.345);self.assertEqual(doc['pixel_sha256'],hashlib.sha256(original.tobytes()).hexdigest())
        self.assertEqual(doc['metadata']['frame_number'],17)
    def test_no_new_inference_has_empty_masks_not_a_previous_mask(self):
        r=self.recorder();im=np.zeros((8,8,3),np.uint8)
        a=r.submit(im,1.,masks=[{'mask':np.ones((8,8),bool),'class_id':1}],metadata={'mask_state':'current_inference'})
        b=r.submit(im,2.,metadata={'mask_state':'no_new_inference'});r.close()
        self.assertTrue(self.document(r,a)['ai_instances']);self.assertEqual(self.document(r,b)['ai_instances'],[])
        with np.load(r.folder/b['frame_id']/'masks.npz',allow_pickle=False) as z:self.assertEqual(z.files,[])
    def test_rolling_window_never_deletes_other_sessions_or_user_files(self):
        keep=Path(self.temp.name)/'user.png';keep.write_bytes(b'preserve')
        r=self.recorder(max_frames=2);refs=[r.submit(np.full((8,8,3),i,np.uint8),float(i)) for i in range(4)];out=r.close()
        self.assertEqual(out['evicted_frames'],2);self.assertEqual([x['frame_id'] for x in out['frames']],[x['frame_id'] for x in refs[-2:]])
        self.assertEqual(keep.read_bytes(),b'preserve');self.assertTrue(all(x['reason']=='rolling_retention' for x in out['gaps']))
    def test_queue_overflow_is_nonblocking_and_explicit(self):
        r=self.recorder(pending_bytes=192);entered=threading.Event();release=threading.Event();original=r._save
        def slow(item):entered.set();release.wait(2);return original(item)
        with patch.object(r,'_save',side_effect=slow):
            first=r.submit(np.zeros((8,8,3),np.uint8),1.);self.assertTrue(entered.wait(1))
            began=time.monotonic();second=r.submit(np.zeros((8,8,3),np.uint8),2.)
            self.assertLess(time.monotonic()-began,.1);self.assertEqual(second['status'],'dropped')
            release.set();out=r.close()
        self.assertFalse(out['complete']);self.assertEqual(out['dropped_frames'],1);self.assertEqual(out['gaps'][0]['reason'],'queue_capacity')
        self.assertTrue((r.folder/first['frame_id']/'frame.png').is_file())
    def test_storage_failure_preserves_manifest_and_never_raises_to_producer(self):
        r=self.recorder()
        with patch.object(r,'_save',side_effect=OSError('ENOSPC injected')):
            r.submit(np.zeros((8,8,3),np.uint8),3.);out=r.close()
        self.assertFalse(out['complete']);self.assertIn('ENOSPC',out['errors'][0]);self.assertEqual(out['gaps'][0]['reason'],'storage_error')
        self.assertEqual(json.loads((r.folder/'manifest.json').read_text())['frames'],[])
    def test_invalid_context_fails_only_the_recording_and_nonfinite_diagnostics_are_retained(self):
        recorder,error=start(self.temp.name,{'run_id':'../bad'},stream='jig');self.assertIsNone(recorder);self.assertTrue(error)
        r=self.recorder();ref=r.submit(np.zeros((8,8,3),np.uint8),1.,metadata={'cost':float('inf')});self.assertTrue(r.close()['complete'])
        self.assertEqual(self.document(r,ref)['metadata']['cost'],{'non_finite':'inf'})
    def test_inspection_decisions_link_to_exact_inputs_without_affecting_counts(self):
        from so101_teach.episode_inspection import InspectionDecision
        r=self.recorder();check={'station':'linear','target':'2','expected':'cap_added','timeout_seconds':10.}
        baseline=InspectionDecision(check,'B',10.);audited=InspectionDecision(check,'B',10.)
        for at in (10.1,10.5):
            ref=r.submit(np.full((8,8,3),int(at),np.uint8),at)
            row={'quality':'normal','state':'cap_added','product':'B','product_certain':True,'reason':'matched'}
            original={'at':at,'row':row};value={**original,'decision_evidence':{**ref,'step_id':'s'}}
            self.assertEqual(baseline.observe(original,at+.1),audited.observe(value,at+.1));self.assertEqual(baseline.count,audited.count)
        out=r.close();notes=out['decisions'];self.assertEqual([x['passed'] for x in notes],[False,True]);self.assertEqual([x['captured_at'] for x in notes],[10.1,10.5])
    def test_declined_inspection_records_reason_but_exception_is_unchanged(self):
        from so101_teach.episode_inspection import InspectionDecision
        r=self.recorder();d=InspectionDecision({'station':'linear','target':'1','expected':'empty','timeout_seconds':5},'A',10.)
        for at in (10.1,10.5):
            ref=r.submit(np.zeros((8,8,3),np.uint8),at);value={'at':at,'row':{'state':'cap_added','quality':'normal','product':'A','product_certain':True,'reason':'occupied'},'decision_evidence':ref}
            if at==10.1:self.assertFalse(d.observe(value,at+.1))
            else:
                with self.assertRaisesRegex(ValueError,'안착 불합격'):d.observe(value,at+.1)
        out=r.close();self.assertIn('안착 불합격',out['decisions'][-1]['error']);self.assertFalse(out['decisions'][-1]['passed'])
    def test_detector_records_real_capture_and_masks_without_changing_pose(self):
        from so101_teach.domain import ROOT
        from so101_teach.vision_service import MultiDetector
        folder=ROOT/'tests/fixtures/cad-pallet-current';data=json.loads((folder/'fixture.json').read_text());cfg=data['cases']['C001']['jigs']['pallet'];frame=cv2.imread(str(folder/'C001.png'));a=np.load(folder/'C001.npz')
        masks=[{'class_id':int(k),'confidence':float(s),'box':b.tolist(),'mask':m} for k,s,b,m in zip(a['classes'],a['scores'],a['boxes'],a['masks'])]
        catalog=SimpleNamespace(items={'pallet':{'roi':cfg['roi'],'support_height_mm':0.}},revision=0,mesh=lambda key:deepcopy(cfg['mesh']))
        results=[];r=self.recorder(stream='jig')
        for evidence in (None,r):
            detector=MultiDetector(catalog,{**cfg['profile'],'jig_detection':data['settings']});detector.region_model=Mock();detector.region_model.predict.return_value=masks;detector.evidence=evidence
            with patch('so101_teach.vision_service.time.monotonic',return_value=100.):out=detector.process_captured(frame,99.5)
            results.append(out);self.assertEqual(detector.region_model.predict.call_count,1);detector.close()
        reference=results[1].pop('decision_evidence');self.assertEqual(results[0],results[1]);self.assertEqual(self.document(r,reference)['captured_at'],99.5)
        self.assertTrue(self.document(r,reference)['ai_instances'])
    def test_camera_worker_passes_original_capture_clock_without_opening_camera(self):
        from so101_teach.devices import CameraSession
        calls=[]
        class Owner:
            def process(self,frame):raise AssertionError('capture adapter expected')
            def process_captured(self,frame,at,shared=False):calls.append(at);return {'status':'ok'}
        owner=Owner();c=CameraSession(0,processor=owner.process);c.pending_frame=(np.zeros((8,8,3),np.uint8),123.25);c.frame_ready.set()
        with patch('cv2.VideoCapture',side_effect=AssertionError('hardware forbidden')):
            t=threading.Thread(target=c.process_frames);t.start()
            try:
                until=time.monotonic()+1
                while c._observation is None and time.monotonic()<until:time.sleep(.005)
            finally:c.stop.set();c.frame_ready.set();t.join(1)
        self.assertEqual(calls,[123.25]);self.assertEqual(c.observation[2],123.25)
    def test_inspector_reports_actual_masks_once_and_observer_errors_do_not_change_result(self):
        from so101_teach.ai_inspection import ModelInspector
        geometry=Mock();geometry.inspect.return_value=[{'id':'slot','quality':'normal','state':'empty'}]
        provider=Mock();provider.predict.return_value=[{'class_id':1,'confidence':.9,'box':[0,0,8,8],'mask':np.ones((8,8),bool)}]
        inspector=ModelInspector(geometry=geometry,provider=provider);self.addCleanup(inspector.close)
        profile={'jig_detection':{'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}};frame=np.zeros((8,8,3),np.uint8);seen=[]
        with patch('so101_teach.ai_inspection.combine',side_effect=lambda rows,*a,**kw:deepcopy(rows)),patch('time.monotonic',return_value=10.):
            first=inspector.inspect(frame,[{'id':'slot'}],profile,evidence=lambda image,masks,rows:seen.append((image,masks,rows)))
            second=inspector.inspect(frame,[{'id':'slot'}],profile,evidence=Mock(side_effect=OSError('observer failed')))
        self.assertEqual(first,second);self.assertEqual(provider.predict.call_count,2);np.testing.assert_array_equal(seen[0][1][0]['mask'],provider.predict.return_value[0]['mask'])
    def test_pc_compute_returns_same_rows_and_exact_masks_for_optional_evidence(self):
        from so101_teach.pc_compute import RemoteInspector,loads,dumps
        from so101_teach.pc_compute_service import ComputeEngine
        provider=Mock();provider.predict.return_value=[{'class_id':1,'confidence':.9,'box':[0,0,8,8],'mask':np.ones((8,8),bool)}]
        engine=ComputeEngine(provider);self.addCleanup(engine.close);engine.inspector.geometry=Mock();engine.inspector.geometry.inspect.return_value=[{'id':'s','state':'empty','quality':'normal'}]
        class Client:
            def call(self,method,frame=None,**args):return loads(dumps(engine.execute(method,frame,loads(dumps(args)))))
        remote=RemoteInspector(Client());profile={'jig_detection':{'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}};frame=np.zeros((8,8,3),np.uint8);seen=[]
        with patch('so101_teach.ai_inspection.combine',side_effect=lambda rows,*a,**kw:deepcopy(rows)),patch('time.monotonic',return_value=10.):
            first=remote.inspect(frame,[{'id':'s'}],profile)
            second=remote.inspect(frame,[{'id':'s'}],profile,evidence=lambda im,ms,rs:seen.extend(ms))
        self.assertEqual(first,second);self.assertEqual(provider.predict.call_count,2);np.testing.assert_array_equal(seen[0]['mask'],provider.predict.return_value[0]['mask'])
    def test_worker_classification_and_consumer_share_a_reference(self):
        from so101_teach.episode_inspection_runtime import FrameInspection
        r=self.recorder();worker=FrameInspection.__new__(FrameInspection);worker.prepared_inspector=SimpleNamespace(evidence=r);worker.profile={}
        class Inspector:
            supports_evidence=True
            def inspect(self,frame,anchors,profile,**kwargs):
                rows=[{'id':'s','quality':'waiting','reason':'no stable geometry'}];kwargs['evidence'](frame,[],rows);return rows
        image=np.ones((8,8,3),np.uint8);rows=worker.classify(Inspector(),image,42.,[{'id':'s'}],metadata={'generation':7})
        result=worker.evidence_result(worker.frame_evidence_ref,{'at':42.,'row':rows[0]},'step');r.close();doc=self.document(r,result['decision_evidence'])
        self.assertEqual(doc['captured_at'],result['at']);self.assertEqual(doc['metadata']['rows'][0]['reason'],'no stable geometry');self.assertEqual(doc['metadata']['generation'],7)

class EvidenceIntegrationTests(EvidenceFixture,unittest.TestCase):
    # Only the two integration cases below are selected separately by the runner.
    def test_processing_error_retains_exact_frame_without_replacing_error(self):
        from so101_teach.vision_service import MultiDetector
        detector=MultiDetector(SimpleNamespace(items={},revision=0),{})
        r=self.recorder(stream='jig');detector.evidence=r
        with patch.object(detector,'process',side_effect=ValueError('invalid calibration')):
            with self.assertRaisesRegex(ValueError,'invalid calibration') as caught:detector.process_captured(np.ones((8,8,3),np.uint8),14.2)
        ref=caught.exception.decision_evidence;detector.close();doc=self.document(r,ref)
        self.assertEqual(doc['captured_at'],14.2);self.assertEqual(doc['metadata']['error'],'invalid calibration');self.assertFalse(doc['ai_instances'])
    def test_runtime_binds_recording_to_detection_and_finishes_without_motion(self):
        from so101_teach.remote_server import Runtime
        runtime=Runtime.__new__(Runtime);runtime.data_dir=Path(self.temp.name);runtime.profile={'robot_id':'arm2'};runtime.generation=1;runtime.runtime_version={'sha256':'mock'}
        runtime.detector=SimpleNamespace(evidence=None,clear=Mock(return_value=True),seconds=10.,acquisition_seconds=4.,attempts_limit=5)
        runtime.catalog=SimpleNamespace(items={});runtime.camera=SimpleNamespace(observation=None,set_mode=Mock())
        reply=runtime.call('detect_clear',{'decision_evidence':{'run_id':'a'*32,'arm':'arm2'}})
        recorder=runtime.detector.evidence;self.recorders.append(recorder)
        recorder.submit(np.zeros((8,8,3),np.uint8),123.,metadata={'status':'not_found'})
        closed=runtime.call('camera_mode',{'processing_enabled':False,'preview_fps':10});recorder.close()
        self.assertIsNone(runtime.detector.evidence);self.assertNotIn('frames',closed['decision_evidence'])
        self.assertEqual(reply['decision_evidence']['session'],closed['decision_evidence']['session'])
        self.assertEqual(len(json.loads((recorder.folder/'manifest.json').read_text())['frames']),1)
    def test_runner_measure_records_consumed_frame_and_keeps_commands_unchanged(self):
        from tests.test_episode_cli import cli,FakeLink
        clock=[100.];frames=[];source=self.recorder(stream='jig');consumer=self.recorder();image=np.zeros((8,8,3),np.uint8)
        for i in range(2):
            at=100.1+i*.2;reference=source.submit(image,at,metadata={'kind':'jig_processing','sample':i})
            result={'selected':({'metric':{'center_xy_mm':[1.,2.],'yaw_deg':3.}} if i else None),'pose_measured_at':at,'acquisition_completed_attempts':1}
            frames.append({'at':at,'detection':{'by_jig':{'pallet':result},'live_by_jig':{'pallet':result},'decision_evidence':reference}})
        results=[];calls=[]
        for audit in (None,consumer):
            clock[0]=100.;events=[]
            class Link(FakeLink):
                def __init__(self):super().__init__();self.frames=iter(deepcopy(frames))
                def post(self,endpoint,body=None):
                    if endpoint=='/camera':
                        value=next(self.frames);clock[0]=value['at']+.05;return value
                    return super().post(endpoint,body)
            link=Link()
            with patch.object(cli,'ROOT',Path(self.temp.name)),patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=lambda t:None):
                runner=cli.Runner(link,events.append,cli.Control(),20);runner.decision_evidence=audit;runner.check=lambda:({},False)
                bundle={'acquisition_seconds':4.,'acquisition_attempts':5,'hold_seconds':10.,'mesh_hashes':{'pallet':'b'*64}}
                results.append(runner.measure([{'jig_id':'pallet'}],bundle));calls.append([method for method,args in link.calls])
        self.assertEqual(results[0],results[1]);self.assertEqual(calls[0],calls[1]);self.assertNotIn('command',calls[1]);self.assertNotIn('connect',calls[1])
        source.close();out=consumer.close();notes=[n for n in out['decisions'] if n['kind']=='jig_consumer']
        self.assertEqual([n['decision'] for n in notes],['waiting','confirmed']);self.assertEqual(notes[-1]['reference']['frame_id'],frames[-1]['detection']['decision_evidence']['frame_id'])

if __name__=='__main__':unittest.main()
