import json,time,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import cv2
from tests import test_ui as fixtures
from so101_teach.vision_service import MultiDetector
from so101_teach.domain import read_json

class CameraOverviewTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def camera(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-lens';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'input.jpg'))
        a.profile=cfg['profile'];a.catalog.items=cfg['jigs'];a.catalog.revision+=1;a.detector=MultiDetector(a.catalog,a.profile)
        base=time.monotonic()-3.1
        for elapsed in (0,.8,1.6,2.4,3.05):
            with patch('so101_teach.vision_service.time.monotonic',return_value=base+elapsed):result=a.detector.process(frame)
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]]);a.show_page('camera');a.camera=SimpleNamespace(running=True,error=None,observation=(frame,result,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.camera_jig_choice.current(0);a.select_camera_jig();self.root.update();a.draw_camera_frame(time.monotonic());return frame,result
    def test_all_matches_and_names_render_without_changing_active_teaching_jig(self):
        a=self.app;active=a.active_jig;frame,result=self.camera();self.assertEqual(a.active_jig,active)
        self.assertIsNone(a.camera_display_snapshot['jig_id']);self.assertEqual(len(a.camera_display_snapshot['result']['by_jig']),2)
        self.assertTrue(all(a.camera_jig_status.item(key,'tags')==('detected',) for key in a.catalog.items))
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1],'돌출부 확인')
        path=a.save_camera();meta=read_json(path.parent/'metadata'/path.with_suffix('.json').name)
        self.assertIsNone(meta['jig_id']);self.assertEqual(meta['detection']['view'],'all');self.assertEqual(len(meta['detection']['by_jig']),2)
        from PIL import ImageGrab
        for size in ('1280x800','1180x760'):
            self.root.geometry(size);self.root.update();a.draw_camera_frame(time.monotonic());self.root.update()
            self.assertLessEqual(a.camera_jig_status.winfo_rootx()+a.camera_jig_status.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            self.assertGreater(a.camera_canvas.winfo_height(),180)
            ImageGrab.grab().save('/tmp/camera-overview-'+size+'.png')
    def test_missing_live_jig_is_not_reported_from_held_measurement(self):
        frame,result=self.camera();a=self.app;data=deepcopy(result);data['live_by_jig']['pallet']={'selected':None,'candidates':[],'status':'not_found'}
        a.camera_overview_updated_at-=1.1;a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic())
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1],'미검출');self.assertIn('1/2',a.camera_overview_title.get())
        self.assertIsNotNone(a.camera_results()['pallet']['selected'])
    def test_overview_blocks_region_edits_and_jig_choice_opens_individual_view(self):
        self.camera();a=self.app;old=deepcopy(a.roi);self.assertEqual(str(a.roi_mode_choice.cget('state')),'disabled')
        a.roi_press(SimpleNamespace(x=200,y=200));self.assertIsNone(a.roi_start);self.assertEqual(a.roi,old)
        a.camera_jig_choice.current(2);a.select_camera_jig();self.root.update()
        self.assertFalse(a.camera_all.get());self.assertEqual(a.active_jig,list(a.catalog.items)[1]);self.assertFalse(a.camera_overview.winfo_ismapped())
    def test_stale_video_clears_previous_green_status(self):
        frame,result=self.camera();a=self.app;a.camera.observation=(frame,result,time.monotonic()-2)
        self.root.after_cancel(a.job);a.poll()
        self.assertTrue(all(a.camera_jig_status.item(key,'values')[1]=='영상 대기' for key in a.catalog.items));self.assertIsNone(a.camera_display_snapshot)
    def test_held_only_results_are_not_counted_as_current_matches(self):
        frame,result=self.camera();a=self.app;data=deepcopy(result);data.pop('live_by_jig')
        for value in data['by_jig'].values():value['pose_held']=True
        a.camera_overview_updated_at-=1.1;a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic())
        self.assertIn('0/2',a.camera_overview_title.get())
        self.assertTrue(all(a.camera_jig_status.item(key,'values')[1]=='유지값' for key in a.catalog.items))
    def test_freehand_stroke_can_reach_image_edges_for_carrier(self):
        frame,_=self.camera();a=self.app;key=list(a.catalog.items)[1];a.select_camera_jig(key);a.camera_all.set(False);a.change_camera_view();a.roi_mode.set('자율 영역');self.root.update();a.draw_camera_frame(time.monotonic())
        left,top,w,h=a.camera_rect;event=lambda p:SimpleNamespace(x=left+p[0]*w,y=top+p[1]*h)
        points=[[.64,.65],[.81,.37],[1.1,.70],[1.1,1.1],[.85,1.1]]
        a.roi_press(event(points[0]))
        for point in points[1:]:a.roi_move(event(point))
        a.roi_release(event(points[0]));self.assertGreaterEqual(len(a.roi),5)
        self.assertEqual(max(point[0] for point in a.roi),1.);self.assertEqual(max(point[1] for point in a.roi),1.)
        self.assertEqual(read_json(self.data/'jigs.json')[key]['roi'],a.roi)
        result=a.detector.process(frame);self.assertIsNotNone(result['live_by_jig'][key]['selected'])
        a.camera.observation=(frame,result,time.monotonic())
        self.root.update();a.draw_camera_frame(time.monotonic());self.root.update()
        from PIL import ImageGrab
        ImageGrab.grab().save('/tmp/camera-freehand-edge.png')
