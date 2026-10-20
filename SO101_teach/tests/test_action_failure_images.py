"""Failure evidence uses received frames, preserves causality and never drives hardware."""
import base64,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
import cv2,numpy as np
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from failure_images import FailureImages


def jpeg(value):
    ok,data=cv2.imencode('.jpg',np.full((8,12,3),value,np.uint8));assert ok
    return base64.b64encode(data).decode(),data.tobytes()


class FailureImageTests(unittest.TestCase):
    def test_decision_image_is_saved_with_its_timestamp_and_separate_newer_preview(self):
        first,raw=jpeg(30);latest,new_raw=jpeg(220)
        with tempfile.TemporaryDirectory() as temp,patch('failure_images.time.monotonic',return_value=12.):
            cache=FailureImages(temp);cache.remember({'at':10.,'image':first,'detection':{'status':'ambiguous'}})
            cache.remember({'preview_at':11.,'preview_image':latest})
            self.assertFalse((Path(temp)/'diagnostics').exists())
            path=cache.save('안착 불합격',context={'run_id':'a'*32,'arm':'arm2'},inspection={'result':{'at':10.,'row':{'quality':'abnormal'}}})
            record=json.loads(path.read_text());self.assertTrue(record['inspection_image_matched'])
            self.assertEqual((path.parent/'inspection.jpg').read_bytes(),raw)
            self.assertEqual((path.parent/'last-received.jpg').read_bytes(),new_raw)
            self.assertEqual([v['age_at_failure_seconds'] for v in record['images'] if v['variant']=='raw'],[2.,1.])
            self.assertEqual(record['images'][0]['detection']['status'],'ambiguous')
            self.assertEqual(cache.save('duplicate',context={}),path)
            self.assertEqual(len(list(path.parent.parent.iterdir())),1)

    def test_delayed_detection_does_not_replace_latest_and_evicted_result_is_not_mislabeled(self):
        image,_=jpeg(100)
        with tempfile.TemporaryDirectory() as temp:
            cache=FailureImages(temp,capacity=2)
            for at in (10.,12.,11.):cache.remember({'at':at,'image':image})
            self.assertEqual(list(cache.frames),[11.,12.])
            path=cache.save('time out',context={},inspection={'result':{'at':10.}})
            record=json.loads(path.read_text());self.assertFalse(record['inspection_image_matched'])
            self.assertEqual(record['images'][0]['role'],'last-received')
            self.assertEqual(record['images'][0]['captured_monotonic'],12.)
            self.assertIsNotNone(record['image_missing_reason'])

    def test_no_received_image_records_reason_without_opening_a_camera(self):
        with tempfile.TemporaryDirectory() as temp,patch('cv2.VideoCapture') as camera:
            cache=FailureImages(temp);cache.remember({'error':'camera unavailable'})
            path=cache.save('connection failed',context={'phase':'preparation'})
            record=json.loads(path.read_text());self.assertEqual(record['images'],[])
            self.assertEqual(record['camera_error'],'camera unavailable');self.assertTrue(record['image_missing_reason'])
            camera.assert_not_called()

    def test_corrupt_jpeg_does_not_discard_failure_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            cache=FailureImages(temp);cache.remember({'preview_at':10.,'preview_image':'not base64!'})
            path=cache.save('original failure',context={});record=json.loads(path.read_text())
            self.assertEqual(record['error'],'original failure');self.assertIsNone(record['images'][0]['file'])
            self.assertIn('save_error',record['images'][0])

    def test_batch_failure_preserves_the_exact_result_consumed_before_worker_advances(self):
        from so101_teach.episode_inspection_runtime import InspectionBatch
        check={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5.}
        old={'at':10.1,'error':'classifier failed'};new={'at':10.2,'row':None};calls=[]
        class Worker:
            @property
            def result(self):calls.append(True);return old if len(calls)==1 else new
            def close(self):pass
        with patch('time.monotonic',return_value=10.2):
            batch=InspectionBatch('A',lambda step:Worker(),lambda step:None);batch.start({'id':'s','name':'검사','inspection':check})
            with self.assertRaises(ValueError) as caught:batch.poll()
            self.assertEqual(caught.exception.inspection_snapshot['result'],old)
            self.assertEqual(len(calls),1);batch.close()

    def test_startup_failure_keeps_the_consumed_group_frame_after_worker_closes(self):
        check={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':5.,'minimum_observations':2}
        steps=[{'id':key,'name':key,'inspection':dict(check),'inspection_product':'A'} for key in ('one','two')]
        def result(at,**changes):
            return {'at':at,'row':{'quality':'normal','state':'housing_seated','product':'A','product_certain':True,'reason':'관측 결과',**changes}}
        image,raw=jpeg(140);now=[100.];events=[]
        class Worker:
            def __init__(self,*args,**kwargs):self.result=None
            def submit(self,frame,at):self.result={'at':at,'results':{s['id']:result(at,quality='abnormal') for s in steps}}
            def retain(self,ids):pass
            def close(self):self.result=None
        class Link(FakeLink):
            def post(self,endpoint,body=None):
                if endpoint=='/camera':return {'preview_at':now[0],'preview_image':image}
                return super().post(endpoint,body)
        with tempfile.TemporaryDirectory() as temp,patch.object(cli,'ROOT',Path(temp)),patch.object(cli.time,'monotonic',side_effect=lambda:now[0]),patch.object(cli.time,'sleep',side_effect=lambda t:now.__setitem__(0,now[0]+t)),patch('so101_teach.configuration.JigCatalog'),patch('so101_teach.workcell_placement.load_placement',return_value={'linear_stage':{}}),patch('so101_teach.episode_inspection_runtime.FrameInspections',Worker):
            runner=cli.Runner(Link(),events.append,cli.Control(),20,linear=FakeLinear())
            runner.inspection_episode='e';runner.inspection_product='A';runner.inspection_records=[];runner.inspection_log=Path(temp)/'inspection.json'
            with self.assertRaisesRegex(ValueError,'불합격') as caught:runner.inspect_startup_group(steps,{'profile':{}})
            runner.save_failure_images(caught.exception)
            record=json.loads(runner.failure_images.saved.read_text())
            self.assertTrue(record['inspection_image_matched'])
            self.assertEqual(record['inspection']['result']['results']['one']['row']['quality'],'abnormal')
            self.assertEqual((runner.failure_images.saved.parent/'inspection.jpg').read_bytes(),raw)
            self.assertIn('failure_evidence',json.loads(runner.inspection_log.read_text()))


class ActionEntryFailureTests(unittest.TestCase):
    def run_failure(self, temp, *, save_error=False):
        frames,raw=jpeg(170);links=[];now=[100.];events=[]
        class Link(FakeLink):
            def __init__(self,arm):super().__init__();self.arm=arm;links.append(self)
            def open(self):pass
            def close(self):pass
            def post(self,endpoint,body=None):
                if endpoint=='/camera':
                    if self.preparing:return super().post(endpoint,body)
                    return {'at':now[0],'image':frames,'detection':{'by_jig':{}}}
                return super().post(endpoint,body)
        class Linear(FakeLinear):
            def open(self):pass
            def close(self):pass
        folder=Path(temp);(folder/'data').mkdir(exist_ok=True)
        episode={'id':'e'*32,'name':'진단 검사','steps':[{'id':'s','name':'work','ticks':{},'jig_id':'j'}]}
        bundle={'profile':{},'acquisition_seconds':.1,'acquisition_attempts':1,'hold_seconds':5,'mesh_hashes':{'j':'mesh'}}
        original=FailureImages.save
        def save(cache,*args,**kwargs):
            self.assertEqual([a['action'] for m,a in links[0].calls if m=='command'][-1],'hold')
            events.append('save')
            if save_error:raise OSError('disk full')
            return original(cache,*args,**kwargs)
        with patch.object(cli,'ROOT',folder),patch.object(cli,'load_job',return_value=(folder/'data',episode,bundle,400,20)),patch.object(cli,'Link',Link),patch('linear_client.LinearClient',Linear),patch.object(cli,'control_server'),patch.object(cli.signal,'signal'),patch.object(cli.time,'monotonic',side_effect=lambda:now[0]),patch.object(cli.time,'sleep',side_effect=lambda t:now.__setitem__(0,now[0]+t)),patch.object(FailureImages,'save',save):
            self.assertEqual(cli.main(['build_a','--no-ros']),1)
        status=json.loads((folder/'data/episode-cli-arm2.json').read_text())
        self.assertIn('FAILED:지그 측정 실패',status['status']);self.assertEqual(events,['save'])
        # Preparation can enable holding torque before measuring jigs. A failed
        # acquisition must still HOLD before saving and never dispatch motion.
        self.assertFalse(any(m=='command' and a.get('action') in ('move','play') for link in links for m,a in link.calls))
        return list((folder/'data/diagnostics/action-failures').glob('*/error.json')),raw

    def test_action_timeout_saves_received_detection_after_hold_even_after_camera_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            files,raw=self.run_failure(temp);self.assertEqual(len(files),1)
            record=json.loads(files[0].read_text());self.assertEqual(record['context']['phase'],'jig_detection')
            self.assertEqual(record['context']['command'],'build_a')
            self.assertEqual((files[0].parent/'detection.jpg').read_bytes(),raw)
            run=json.loads(next((Path(temp)/'data/episode-cli-runs').glob('*.json')).read_text())
            self.assertIn('FAILURE_IMAGES:',json.dumps(run))

    def test_disk_failure_preserves_action_error_and_stop_handling(self):
        with tempfile.TemporaryDirectory() as temp:
            files,_=self.run_failure(temp,save_error=True);self.assertEqual(files,[])

class PairedFailureImageTests(unittest.TestCase):
    def large_jpeg(self,value=100):
        ok,encoded=cv2.imencode('.jpg',np.full((480,640,3),value,np.uint8));assert ok
        return base64.b64encode(encoded).decode(),encoded.tobytes()
    def test_raw_and_annotation_share_decision_frame_and_newer_frame_gets_no_old_verdict(self):
        image,raw=self.large_jpeg();preview,_=self.large_jpeg(180)
        row={'quality':'abnormal','state':'empty','label':'부품 없음','reason':'검증용 판정','roi_display_polygon':[[100,100],[300,100],[300,300],[100,300]],'observed_polygon':[]}
        with tempfile.TemporaryDirectory() as temp:
            cache=FailureImages(temp);cache.remember({'at':10.,'image':image});cache.remember({'preview_at':11.,'preview_image':preview})
            path=cache.save('안착 불합격',context={},inspection={'result':{'at':10.,'row':row}});record=json.loads(path.read_text());entries={i['role']:i for i in record['images']}
            self.assertEqual((path.parent/'inspection.jpg').read_bytes(),raw)
            a=entries['inspection-annotated'];self.assertEqual(a['source_file'],'inspection.jpg');self.assertEqual(a['captured_monotonic'],10.)
            self.assertEqual(a['annotation']['decision_rows'],1);self.assertEqual(entries['last-received-annotated']['annotation']['decision_rows'],0)
            shown=cv2.imread(str(path.parent/a['file']));self.assertEqual(shown.shape,(480,640,3));self.assertGreater(int(shown[100,200,2]),int(shown[100,200,1]))
            from so101_teach.failure_archive import read_record
            fetched=read_record(temp,'evidence:'+path.parent.name);self.assertEqual(len(fetched['images']),4)
    def test_confirmed_frame_and_pose_survive_normal_cache_eviction(self):
        image,raw=self.large_jpeg();quad=[[100,100],[200,100],[200,200],[100,200]]
        with tempfile.TemporaryDirectory() as temp:
            cache=FailureImages(temp,capacity=1);cache.remember({'at':10.,'image':image,'detection':{'by_jig':{'j':{'selected':{'quad':quad}}}}})
            poses={'j':{'pose':[1.,2.,3.]}};cache.confirm_jigs(poses);poses['j']['pose'][0]=999
            cache.remember({'preview_at':20.,'preview_image':image});self.assertNotIn(10.,cache.frames)
            path=cache.save('motion error',context={});doc=json.loads(path.read_text());entries={i['role']:i for i in doc['images']}
            self.assertEqual((path.parent/'jig-confirmed.jpg').read_bytes(),raw)
            self.assertEqual(entries['jig-confirmed']['captured_monotonic'],10.)
            self.assertEqual(entries['jig-confirmed']['confirmed_jigs']['j']['pose'],[1.,2.,3.])
            self.assertIsNotNone(entries['jig-confirmed-annotated']['file'])
    def test_overlay_failure_keeps_raw_and_original_error(self):
        image,raw=self.large_jpeg()
        with tempfile.TemporaryDirectory() as temp,patch('so101_teach.failure_overlay.render',side_effect=ValueError('overlay failed')):
            cache=FailureImages(temp);cache.remember({'preview_at':10.,'preview_image':image});path=cache.save('motor fault',context={});doc=json.loads(path.read_text())
            self.assertEqual(doc['error'],'motor fault');self.assertEqual((path.parent/'last-received.jpg').read_bytes(),raw)
            self.assertIsNone(doc['images'][1]['file']);self.assertEqual(doc['images'][1]['save_error'],'overlay failed')
    def test_same_frame_metadata_update_is_used_without_requesting_new_image(self):
        image,_=self.large_jpeg()
        with tempfile.TemporaryDirectory() as temp:
            cache=FailureImages(temp);cache.remember({'at':10.,'image':image,'detection':{'status':'waiting'}})
            cache.remember({'at':10.,'detection':{'status':'confirmed'}});cache.confirm_jigs({'j':{'pose':[1.,2.,3.]}})
            self.assertEqual(cache.confirmed['detection']['status'],'confirmed')

    def test_packaged_korean_font_renders_when_system_fonts_are_absent(self):
        from so101_teach.vision import roi_font
        from so101_teach.inspection_overlay import label_image
        original_exists=Path.exists;roi_font.cache_clear();label_image.cache_clear()
        try:
            with patch('so101_teach.vision.Path.exists',lambda path:False if str(path).startswith('/usr/share/fonts/') else original_exists(path)):
                font=roi_font();self.assertIsNotNone(font);self.assertIn('Nanum',font.getname()[0])
                image=label_image('검사 판정 · 부품 없음');self.assertGreater(image.shape[1],100)
        finally:roi_font.cache_clear();label_image.cache_clear()
