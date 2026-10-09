import unittest
from unittest.mock import patch
from tests import test_camera_overview as fixtures
from so101_teach.adoption_display import adoption_summary
class ReadabilityTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_fast_result_updates_are_limited_to_one_per_second(self):
        a=self.app;key=next(iter(a.catalog.items));a.camera_overview_updated_at=-10
        with patch('so101_teach.ui.time.monotonic',return_value=10):a.update_camera_overview({key:{'selected':{},'status':'processing'}})
        with patch('so101_teach.ui.time.monotonic',return_value=10.2):a.update_camera_overview({key:{'selected':{'grid_verified':True}}})
        self.assertEqual(a.camera_jig_status.item(key,'values')[1],'갱신 중')
        with patch('so101_teach.ui.time.monotonic',return_value=11.1):a.update_camera_overview({key:{'selected':{'grid_verified':True}}})
        self.assertEqual(a.camera_jig_status.item(key,'values')[1],'교차점 확인')
        with patch('so101_teach.ui.time.monotonic',return_value=11.2):a.update_camera_overview({key:{'status':'error'}})
        self.assertEqual(a.camera_jig_status.item(key,'values')[1],'검출 오류')
        a.update_camera_overview({});self.assertEqual(a.camera_jig_status.item(key,'values')[1],'영상 대기')
    def test_compact_adoption_preserves_full_reason_below_table(self):
        a=self.app;key=next(iter(a.catalog.items));a.show_adoption.set(True);a.refresh_adoption_display()
        full='5회차 실패 · 관측 부족 · 새 관측 1회'
        with patch.object(a,'adoption_labels',return_value={key:full}):a.update_camera_overview({key:{'status':'processing'}})
        self.assertEqual(a.camera_jig_status.item(key,'values')[2],'5회차 실패')
        a.camera_jig_status.selection_set(key);a.show_camera_overview_detail();self.assertIn(full,a.camera_overview_detail.get())
        self.assertEqual(adoption_summary('2/5회차 · 수집 1.4/4초 · 새 관측 3회'),'2/5회차 수집 중')
        self.assertEqual(adoption_summary('채택됨 · 유지 3.2초'),'채택 유지')
    def test_full_width_table_and_detail_fit_maximized_screen(self):
        from PIL import ImageGrab
        from so101_teach.domain import ROOT
        frame,data=self.camera();a=self.app;self.root.geometry('1920x1043');a.show_adoption.set(True);a.refresh_adoption_display();a.camera_overview_updated_at=-10
        full={key:'5회차 실패 · 관측 부족 · 새 관측 1회' for key in a.catalog.items}
        with patch.object(a,'adoption_labels',return_value=full):a.update_camera_overview(a.camera_view_results(data))
        a.camera_jig_status.selection_set(next(iter(a.catalog.items)));a.show_camera_overview_detail();self.root.update()
        self.assertEqual(a.camera_jig_status.xview(),(0.,1.))
        self.assertLess(a.camera_overview.winfo_rootx()+a.camera_overview.winfo_width(),1920)
        out=ROOT/'verification/camera-status-readability';out.mkdir(exist_ok=True)
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+1920,y+1043)).save(out/'after.png')
