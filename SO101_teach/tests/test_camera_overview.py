import json,time,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import cv2
from tests import test_ui as fixtures
from so101_teach.vision_service import MultiDetector
from so101_teach.domain import ROOT,read_json

class CameraOverviewTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def camera(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-lens';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'input.jpg'))
        a.profile=cfg['profile'];a.catalog.items=cfg['jigs']
        for item in a.catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        a.catalog.revision+=1;a.detector=MultiDetector(a.catalog,a.profile)
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
        path=a.save_camera();meta=read_json(path.parent.parent/'판정 기록'/path.with_suffix('.json').name)
        self.assertIsNone(meta['jig_id']);self.assertEqual(meta['detection']['view'],'all');self.assertEqual(len(meta['detection']['by_jig']),2)
        from PIL import ImageGrab
        for size in ('1920x1043','1920x1080'):
            self.root.geometry(size);self.root.update();a.draw_camera_frame(time.monotonic());self.root.update()
            self.assertLessEqual(a.camera_jig_status.winfo_rootx()+a.camera_jig_status.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            self.assertGreaterEqual(a.camera_canvas.winfo_height(),470)
            ImageGrab.grab().save('/tmp/camera-overview-'+size+'.png')
    def test_missing_live_jig_is_not_reported_from_held_measurement(self):
        frame,result=self.camera();a=self.app;data=deepcopy(result);data['live_by_jig']['pallet']={'selected':None,'candidates':[],'status':'not_found'}
        a.camera_overview_updated_at-=1.1;a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic())
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1],'미검출');self.assertIn('1/2',a.camera_overview_title.get())
        self.assertIsNotNone(a.camera_results()['pallet']['selected'])
    def test_rejected_label_shows_cause_and_all_roi_boundaries_without_adoption(self):
        from so101_teach.vision import annotate_rois
        import numpy as np
        frame,result=self.camera();a=self.app;data=deepcopy(result)
        rejection={'selected':None,'candidates':[],'status':'mask_rim_not_found','cad_pallet_reason':'roi',
                   'model_detected':True,'error':'팔레트가 지그 검출 ROI 범위를 벗어남 · 범위 안 77.5% (필요 98%)'}
        data['live_by_jig']['pallet']=deepcopy(rejection)
        data['by_jig']['pallet']={**rejection,'status':'confirming','stable_candidate_seconds':.2,
                                  'acquisition_seconds':4,'stable_candidate_samples':0}
        before=deepcopy(data);a.camera.observation=(frame,data,time.monotonic())
        a.show_adoption.set(True);a.toggle_adoption_display();a.camera_overview_updated_at-=1.1
        a.draw_camera_frame(time.monotonic());a.camera_jig_status.selection_set('pallet');a.show_camera_overview_detail()
        self.root.update()
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1:3],('ROI 경계 이탈','ROI 경계 이탈'))
        self.assertIn('AI 지그 라벨 확인',a.camera_overview_detail.get())
        self.assertIn('77.5%',a.camera_overview_detail.get())
        snap=a.camera_display_snapshot
        self.assertEqual(snap['result']['roi_by_jig'],{k:j.get('roi')for k,j in a.catalog.items.items()})
        self.assertFalse(np.array_equal(snap['base_image'],snap['image']))
        np.testing.assert_array_equal(snap['image'],annotate_rois(snap['base_image'],a.catalog.items))
        self.assertEqual(data,before)
        path=a.save_camera();meta=read_json(path.parent.parent/'판정 기록'/path.with_suffix('.json').name)
        self.assertEqual(meta['detection']['roi_by_jig'],snap['result']['roi_by_jig'])
        from PIL import ImageGrab
        ImageGrab.grab().save('/tmp/jig-rejection-overview.png')
        # A single-jig view must retain the cause even if only the acquisition
        # result (without a separate live result) is available.
        data.pop('live_by_jig');a.camera_all.set(False);a.change_camera_view()
        a.camera.observation=(frame,data,time.monotonic())
        self.root.after_cancel(a.job);a.poll()
        self.assertIn('채택 불가:',a.camera_status.get())
        self.assertIn('77.5%',a.camera_status.get())
    def test_overview_blocks_region_edits_and_jig_choice_opens_individual_view(self):
        self.camera();a=self.app;old=deepcopy(a.roi);self.assertEqual(str(a.roi_mode_choice.cget('state')),'disabled')
        catalog=deepcopy(a.catalog.items);revision=a.catalog.revision;generation=a.detector.generation
        self.assertTrue(a.roi_clear_button.instate(['disabled']))
        a.roi_press(SimpleNamespace(x=200,y=200));self.assertIsNone(a.roi_start);self.assertEqual(a.roi,old)
        a.apply_camera_roi([.1,.1,.9,.9]);a.clear_camera_roi()
        self.assertEqual(a.roi,old);self.assertEqual(a.catalog.items,catalog)
        self.assertEqual(a.catalog.revision,revision);self.assertEqual(a.detector.generation,generation)
        a.camera_jig_choice.current(2);a.select_camera_jig();self.root.update()
        self.assertFalse(a.camera_all.get());self.assertEqual(a.active_jig,list(a.catalog.items)[1]);self.assertFalse(a.camera_overview.winfo_ismapped())
        self.assertEqual(str(a.roi_mode_choice.cget('state')),'readonly')
        self.assertFalse(a.roi_clear_button.instate(['disabled']))
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
