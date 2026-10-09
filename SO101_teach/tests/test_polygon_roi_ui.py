import time,unittest,json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import cv2
from tests import test_ui as fixtures
from so101_teach.domain import read_json
from so101_teach.vision import detect

class PolygonROIUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def setup_camera(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-lens';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'input.jpg'));j=cfg['jigs']['pallet'];m={**j['mesh'],'shape':j['shape'],'method':j['method']}
        result=detect(frame,m,j['roi'],cfg['profile']);a.roi=j['roi'];a.save_preferences();a.show_page('camera')
        now=time.monotonic();data={'by_jig':{'pallet':result},'live_by_jig':{'pallet':result}}
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,now),close=lambda:None,join=lambda n:True)
        self.root.update();a.draw_camera_frame(now);self.root.update();return result
    def click(self,point):
        left,top,w,h=self.app.camera_rect;e=SimpleNamespace(x=left+point[0]*w,y=top+point[1]*h)
        self.app.roi_press(e);self.app.roi_release(e)
    def test_freehand_release_saves_region_and_draft_never_changes_detection_region(self):
        self.setup_camera();a=self.app;old=deepcopy(a.roi);a.roi_mode.set('자율 영역')
        left,top,w,h=a.camera_rect
        event=lambda p:SimpleNamespace(x=left+p[0]*w,y=top+p[1]*h)
        points=[[.165,.435],[.255,.34],[.30,.515],[.205,.60]]
        a.roi_press(event(points[0]))
        for point in points[1:]:
            a.roi_move(event(point));self.assertEqual(a.roi,old);self.assertEqual(read_json(self.data/'jigs.json')['pallet']['roi'],old)
        a.roi_release(event(points[0]));self.assertTrue(all(len(p)==2 for p in a.roi));self.assertEqual(a.roi_points,[])
        self.assertEqual(read_json(self.data/'jigs.json')['pallet']['roi'],a.roi)
        self.root.update();self.assertGreater(a.camera_canvas.winfo_height(),180)
    def test_cancel_rectangle_edges_and_crossed_stroke_preserve_old_region(self):
        self.setup_camera();a=self.app;old=deepcopy(a.roi);left,top,w,h=a.camera_rect
        event=lambda x,y:SimpleNamespace(x=left+x*w,y=top+y*h)
        a.roi_press(event(.2,.2));a.roi_move(event(.5,.5));a.cancel_roi_points();a.roi_release(event(.5,.5));self.assertEqual(a.roi,old)
        a.roi_mode.set('자율 영역');a.roi_press(event(.1,.1))
        for p in [[.8,.8],[.8,.1],[.1,.8]]:a.roi_move(event(*p))
        a.roi_release(event(.1,.1));self.assertEqual(a.roi,old)
        a.roi_mode.set('사각형');a.roi_press(event(1.1,.2));a.roi_move(event(.6,1.2));a.roi_release(event(.6,1.2))
        self.assertAlmostEqual(a.roi[0],.6);self.assertEqual(a.roi[1:],[.2,1.,1.])
    def test_roi_edits_are_blocked_during_measurement_or_execution(self):
        self.setup_camera();a=self.app;old=deepcopy(a.roi);a.pose_latch.freeze(True)
        self.click([.2,.2]);a.clear_camera_roi();self.assertEqual(a.roi,old);self.assertEqual(a.roi_points,[])
        a.pose_latch.freeze(False);a.camera_task={'kind':'photo'}
        self.click([.2,.2]);self.assertEqual(a.roi_points,[]);a.camera_task=None
    def test_latest_detection_disappears_on_view_but_teaching_reference_is_retained(self):
        strong=self.setup_camera();a=self.app;frame,data,at=a.camera.observation;held={**strong,'pose_held':True,'hold_remaining_s':10}
        missing={'selected':None,'candidates':[],'status':'not_found'}
        a.camera.observation=(frame,{'by_jig':{'pallet':held},'live_by_jig':{'pallet':missing}},time.monotonic())
        a.draw_camera_frame(time.monotonic());self.assertIsNone(a.camera_display_snapshot['result']['selected'])
        self.assertIsNotNone(a.camera_results()['pallet']['selected'])
    def test_newer_preview_is_rendered_even_before_a_detection_is_available(self):
        self.setup_camera();a=self.app;frame,data,at=a.camera.observation
        a.camera.preview_observation=(frame,{'selected':None,'candidates':[],'live_by_jig':{}},time.monotonic());a.camera.observation=None
        a.draw_camera_frame(time.monotonic());self.assertIsNotNone(a.camera_display_snapshot)
        self.assertIsNone(a.camera_display_snapshot['result']['selected']);self.assertTrue(a.camera_display_snapshot['result']['live_view'])
    def test_remote_sync_observes_new_polygon_before_clearing_measurement(self):
        from unittest.mock import patch
        self.setup_camera();a=self.app;region=[[.165,.435],[.255,.34],[.30,.515],[.205,.60]]
        def sync_and_clear(key):
            self.assertEqual(a.catalog.items[key]['roi'],region)
            self.assertEqual(read_json(self.data/'jigs.json')[key]['roi'],region)
            return True
        with patch.object(a.detector,'clear',side_effect=sync_and_clear):a.apply_camera_roi(region)
