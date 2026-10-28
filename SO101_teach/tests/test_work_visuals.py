"""Inspection evidence, frame identity, and camera-only viewing boundaries."""
import json,tempfile,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from copy import deepcopy
import numpy as np
from PIL import Image
from integration.work_visuals import WorkVisuals
from desktop_monitor.source import Source
from desktop_monitor.overlays import inspection_marks,visual_for
from desktop_monitor.model import adopted_outlines
from tests.test_work_monitor_model import fixture


def visual_sample():
    s=fixture();now=time.monotonic();wall=time.time();run=s['runs'][0]
    run['expected']['arm2'][0]['check'].update(station='linear',target='1')
    for i,row in enumerate(s['events']):row['at']=wall-4+i
    run.update(started=wall-4,at=wall-1)
    roi=[[300,250],[500,250],[500,450],[300,450]]
    observation={'step_id':'step','step_name':'하단 확인','check':run['expected']['arm2'][0]['check'],
        'product':'A','captured_at':now-.5,'captured_wall_time':wall-.5,'age_s':.5,
        'row':{'quality':'normal','label':'A 하단 · 정상','state':'housing_seated','product':'A','product_certain':True,'reason':'안착 영역 안',
               'roi_display_polygon':roi,'observed_polygon':[[330,280],[470,280],[470,420],[330,420]]}}
    s.update(connected=True,received=now,at=wall,camera={'arm':'arm2','running':True,'frozen':{}},
        frame=Image.new('RGB',(1280,720),'#819493'),frame_key=('camera',1),frame_received=now,frame_age=.1,
        inspection_frame=Image.new('RGB',(960,540),'#819493'),inspection_key=('v',1),
        visual={'schema':1,'run_id':run['run_id'],'job_id':run['job_id'],'arm':'arm2','instance':'v','sequence':1,
                'captured_at':now-.5,'age_s':.5,'source_size':[1280,720],'frame_checks':['step'],'checks':{'step':observation}})
    return s


class VisualModelTests(unittest.TestCase):
    def test_valid_empty_observation_needs_recorded_pass_and_keeps_roi(self):
        s=visual_sample();r=s['runs'][0];item=s['visual']['checks']['step']
        item['check']['expected']='empty';item['row'].update(state='empty',quality='abnormal',seating='missing',product_certain=False)
        self.assertEqual(inspection_marks(s,r)[0]['tone'],'pending')
        s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':'A_INSPECTION_PASS:step'})
        for evidence in (False,True):
            marks=inspection_marks(s,r,evidence=evidence)
            self.assertEqual(len(marks),1);self.assertEqual(marks[0]['tone'],'pass');self.assertTrue(marks[0]['region'])
    def test_occupied_pass_is_distinct_from_seated_and_old_fault_stays_visible(self):
        s=visual_sample();r=s['runs'][0];item=s['visual']['checks']['step']
        item['check']['expected']='occupied';item['row'].update(quality='abnormal',seating='boundary')
        s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':'A_INSPECTION_PASS:step'})
        self.assertEqual(inspection_marks(s,r,evidence=True)[0]['tone'],'pass')
        item['row']['seating']='outside';item['row']['quality']='outside'
        self.assertEqual(inspection_marks(s,r),[])
        self.assertEqual(inspection_marks(s,r,evidence=True)[0]['tone'],'error')
    def test_old_normal_observation_cannot_borrow_a_different_expected_pass(self):
        s=visual_sample();r=s['runs'][0];s['visual']['checks']['step']['check']['expected']='cap_added'
        s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':'A_INSPECTION_PASS:step'})
        self.assertEqual(inspection_marks(s,r),[])
        self.assertNotEqual(inspection_marks(s,r,evidence=True)[0]['tone'],'pass')
    def test_normal_classification_is_not_a_temporal_pass(self):
        s=visual_sample();r=s['runs'][0]
        mark=inspection_marks(s,r)[0]
        self.assertEqual(mark['state'],'검사 중');self.assertEqual(mark['tone'],'pending');self.assertEqual(mark['contours'],[])
        self.assertTrue(inspection_marks(s,r,evidence=True)[0]['contours'])
        s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':'B_INSPECTION_PASS:step'})
        self.assertEqual(inspection_marks(s,r)[0]['state'],'통과')
    def test_stale_regions_hide_on_live_video_but_remain_historical_evidence(self):
        s=visual_sample();s['visual']['checks']['step']['age_s']=6
        self.assertEqual(inspection_marks(s,s['runs'][0]),[])
        self.assertEqual(len(inspection_marks(s,s['runs'][0],evidence=True)),1)
    def test_changed_job_and_camera_cannot_inherit_geometry(self):
        s=visual_sample();s['visual']['job_id']='old'
        self.assertEqual(inspection_marks(s,s['runs'][0],evidence=True),[])
        s=visual_sample();s['camera']['arm']='arm3';self.assertEqual(inspection_marks(s,s['runs'][0]),[])
    def test_linear_move_or_new_inspection_invalidates_previous_live_region(self):
        for status in ('B_LINEAR_STARTED:target_mm=100','B_INSPECTION_STARTED:step:재검사'):
            s=visual_sample();r=s['runs'][0]
            s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':status})
            self.assertEqual(inspection_marks(s,r),[])
    def test_evidence_contours_require_the_exact_frame_timestamp(self):
        s=visual_sample();s['visual']['checks']['step']['captured_at']-=1
        self.assertEqual(inspection_marks(s,s['runs'][0],evidence=True),[])
    def test_recorded_adoption_survives_detector_pause_and_is_cleared_on_remeasurement(self):
        s=visual_sample();r=s['runs'][0]
        r['poses']=[{'arm':'arm2','adopted_at':time.time()-2,'image_size':[1280,720],
            'poses':[{'jig_id':'jig','x_mm':1,'y_mm':2,'yaw_deg':3,'quad':[[10,10],[100,10],[100,90],[10,90]]}]}]
        self.assertEqual(len(adopted_outlines(s,r)),1)
        s['events'].append({'at':time.time(),'arm':'arm2','run_id':r['run_id'],'sequence':5,'status':'B_JIG_RECHECK_AFTER_RESUME'})
        self.assertEqual(adopted_outlines(s,r),[])


class VisualWriterTests(unittest.TestCase):
    def test_writer_preserves_capture_pair_and_source_only_sends_new_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir();now=time.monotonic()
            writer=WorkVisuals(root/'data',arm='arm2',job_id='job',run_id='a'*32,episode_id='e')
            frame=np.full((72,128,3),80,np.uint8)
            check={'station':'linear','target':'1','expected':'housing_seated','timeout_seconds':10}
            writer.submit(frame,now,[{'step_id':'s','check':check,'product':'A','row':{
                'quality':'normal','state':'housing_seated','product':'A','product_certain':True,
                'roi_polygon':[[1,1],[50,1],[50,50]]}}]);writer.close()
            value=json.loads(writer.path.read_text());self.assertEqual(value['captured_at'],now)
            self.assertEqual(value['checks']['s']['captured_at'],now);self.assertEqual(value['source_size'],[128,72]);self.assertIn('image',value)
            run={'run_id':'a'*32,'job_id':'job','arm':'arm2'};source=Source(root)
            a=source.visual({'info':None},[run]);b=source.visual({'info':None},[run])
            self.assertIn('image',a);self.assertNotIn('image',b)
            from so101_teach.episode_inspection import matches_expected
            self.assertTrue(matches_expected(a['checks']['s']['row'],check,'A'))
            self.assertEqual(source.visual({'info':None},[{**run,'run_id':'b'*32}]),{})
    def test_visual_failure_does_not_mutate_inspection_result(self):
        from so101_teach.episode_inspection_runtime import FrameInspection
        observed={'at':1.,'row':{'quality':'normal'}};before=deepcopy(observed)
        writer=SimpleNamespace(submit=Mock(side_effect=OSError('disk full')),error=None)
        worker=FrameInspection.__new__(FrameInspection)
        worker.prepared_inspector=SimpleNamespace(visual=writer);worker.product='A';worker.visual_step={'id':'s','inspection':{}}
        worker.publish_visual(np.zeros((4,4,3),np.uint8),1.,{'s':observed})
        self.assertEqual(observed,before);self.assertEqual(writer.error,'disk full')
    def test_old_parallel_result_cannot_replace_newer_evidence_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            writer=WorkVisuals(folder,arm='arm2',job_id='j',run_id='a'*32,episode_id='e');frame=np.zeros((8,8,3),np.uint8);now=time.monotonic()
            writer.submit(frame,now,[{'step_id':'new','row':{}}]);writer.submit(frame,now-1,[{'step_id':'old','row':{}}]);writer.close()
            self.assertEqual(json.loads(writer.path.read_text())['captured_at'],now)


class Camera:
    def __init__(self,**kwargs):self.running=False;self.mode=None;self.closed=0
    def start(self):self.running=True
    def close(self):self.closed+=1;self.running=False
    def join(self,timeout):return True
    def set_mode(self,**mode):self.mode=mode


class PreviewWatchTests(unittest.TestCase):
    def setUp(self):
        from so101_teach.preview_watch import PreviewWatch
        self.factory=Mock(side_effect=Camera)
        self.r=SimpleNamespace(robot_id=None,lock=threading.RLock(),lease=None,detached=True,camera=None,
            camera_factory=self.factory,profile={'camera':{'source':'fake','width':1280,'height':720}})
        self.watch=PreviewWatch(self.r);self.first='1'*32;self.second='2'*32
    def request(self,viewer=None,operation='watch'):return self.watch.call({'operation':operation,'viewer':viewer or self.first})
    def test_idle_view_starts_only_preview_without_a_robot_lease(self):
        self.request();camera=self.r.camera
        self.assertTrue(camera.running);self.assertFalse(camera.mode['processing_enabled']);self.assertEqual(camera.mode['preview_fps'],10);self.assertIsNone(self.r.lease)
        self.request(operation='release');self.assertFalse(camera.running)
    def test_closing_one_of_two_viewers_does_not_stop_the_other(self):
        self.request();self.request(self.second);camera=self.r.camera
        self.request(operation='release');self.assertTrue(camera.running)
        self.request(self.second,'release');self.assertFalse(camera.running)
    def test_lost_viewer_expires_without_touching_motor_state(self):
        self.request();camera=self.r.camera
        with patch('so101_teach.preview_watch.time.monotonic',return_value=time.monotonic()+5):self.watch.expire()
        self.assertFalse(camera.running);self.assertIsNone(self.r.lease)
    def test_viewer_does_not_change_or_close_an_owner_camera(self):
        camera=Camera();camera.start();camera.set_mode(processing_enabled=True,preview_fps=10)
        self.r.camera=camera;self.r.lease='executor';self.r.detached=False
        self.request();self.request(operation='release')
        self.assertTrue(camera.running);self.assertEqual(camera.mode,{'processing_enabled':True,'preview_fps':10});self.factory.assert_not_called()
    def test_control_acquisition_yields_only_passive_preview(self):
        self.request();camera=self.r.camera;self.watch.begin_acquire()
        self.assertFalse(camera.running);self.assertIsNone(self.watch.owned)
        self.r.lease='executor';self.r.detached=False;self.watch.acquiring=False;self.request()
        self.assertEqual(self.factory.call_count,1)
    def test_peer_control_blocks_idle_camera_opening(self):
        with patch.object(self.watch,'peer',return_value={'controlling':True}):self.request()
        self.factory.assert_not_called()
    def test_preview_endpoint_rejects_unrelated_operations(self):
        for operation in ('move','acquire','configure','camera_mode'):
            with self.assertRaises(ValueError):self.request(operation=operation)


class PreviewRuntimeTests(unittest.TestCase):
    def test_other_arm_acquisition_yields_preview_and_prevents_reopening(self):
        from so101_teach.remote_server import Runtime,Server,Handler
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder);(data/'arm3-runtime').mkdir();runtimes=[];servers=[]
            try:
                for arm,target in (('arm2',data),('arm3',data/'arm3-runtime')):
                    runtime=Runtime(target,robot_id=arm,camera_factory=Camera,motion_factory=Mock(side_effect=AssertionError('No motors')))
                    runtime.profile={'camera':{'source':'fake'}};runtimes.append(runtime)
                    server=Server(('127.0.0.1',0),Handler);server.runtime=runtime;server.token='fixture'
                    threading.Thread(target=server.serve_forever,daemon=True).start();servers.append(server)
                    (target/'remote-server.json').write_text(json.dumps({'port':server.server_port,'pid':0}))
                    (target/'remote-token').write_text('fixture')
                a,b=runtimes;viewer='a'*32
                a.preview_watch.call({'operation':'watch','viewer':viewer});preview=a.camera
                self.assertTrue(preview.running)
                lease=b.acquire()['lease'];self.assertFalse(preview.running)
                a.preview_watch.call({'operation':'watch','viewer':viewer});self.assertIs(a.camera,preview);self.assertFalse(preview.running)
                self.assertEqual(b.lease,lease);self.assertFalse(b.detached)
                a.motion_factory.assert_not_called();b.motion_factory.assert_not_called()
            finally:
                for server in servers:server.shutdown();server.server_close()
                for runtime in runtimes:runtime.close()


class VisualUITests(unittest.TestCase):
    from tests import test_ui as _ui
    setUp=_ui.UITests.setUp
    tearDown=_ui.UITests.tearDown
    def panel(self):
        from tests.test_work_monitor_ui import FakeObserver
        p=self.app.camera_work_monitor;p.factory=FakeObserver
        self.app.camera_tabs.select(p);self.app.show_page('camera');self.root.update()
        return p
    def test_live_regions_and_evidence_contours_switch_with_readable_labels(self):
        p=self.panel();sample=visual_sample();p.consume(sample);self.root.update()
        text=' '.join(p.canvas.itemcget(item,'text') for item in p.canvas.find_withtag('annotation') if p.canvas.type(item)=='text')
        self.assertIn('A 팔레트 · 검사 중',text)
        p.toggle_evidence();self.root.update()
        text=' '.join(p.canvas.itemcget(item,'text') for item in p.canvas.find_withtag('annotation') if p.canvas.type(item)=='text')
        self.assertNotIn('판정 영상',text);self.assertIn('판정 영상',p.camera_status.get());self.assertIn('A 하단 · 정상',text)
        self.assertIn('밝은 실선',p.camera_status.get())
        self.assertLessEqual(p.inspection_button.winfo_rootx()+p.inspection_button.winfo_width(),self.root.winfo_width())
    def test_overlay_toggle_and_expired_live_image_remove_geometry(self):
        p=self.panel();sample=visual_sample();p.consume(sample)
        p.overlays.set(False);p.paint()
        self.assertFalse(any(p.canvas.type(item)=='line' for item in p.canvas.find_withtag('annotation')))
        p.overlays.set(True);p.snapshot['frame_age']=5;p.paint()
        self.assertFalse(p.canvas.find_withtag('annotation'));self.assertFalse(p.canvas.find_withtag('frame'))
    def test_old_job_inspection_image_does_not_appear_in_new_job(self):
        p=self.panel();sample=visual_sample();sample['visual']['job_id']='old';p.consume(sample);p.toggle_evidence()
        self.assertFalse(p.canvas.find_withtag('frame'));self.assertIn('판정 영상',p.camera_status.get())
