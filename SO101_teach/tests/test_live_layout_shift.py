import time,unittest
from unittest.mock import patch
from tests import test_camera_overview as fixtures

class LayoutTests(unittest.TestCase):
    setUp=fixtures.CameraOverviewTests.setUp
    tearDown=fixtures.CameraOverviewTests.tearDown
    camera=fixtures.CameraOverviewTests.camera
    def test_changing_inspection_text_never_resizes_video(self):
        a=self.app;p=a.camera_inspection;self.root.geometry('1920x1043');a.show_page('camera');a.camera_tabs.select(p);self.root.update()
        sizes=[]
        for text in ('','리니어 좌측 A용 팔레트: X -248, Y 104.9','좌측 A / 우측 B\n완성품 팔레트 현재 검출 위치', ''):
            p.position.set(text);p.status.set(text);p.details.set(text);self.root.update_idletasks();sizes.append((p.canvas.winfo_width(),p.canvas.winfo_height()))
        self.assertEqual(len(set(sizes)),1)
    def test_both_monitor_tables_fit_maximized_width(self):
        a=self.app;self.root.geometry('1920x1043');a.show_page('devices');self.root.update()
        for card in a.dual_monitor.cards.values():
            tree=card['tree'];self.assertGreaterEqual(tree.xview()[1],.999)
            scroll=next(c for c in tree.master.winfo_children() if c.winfo_class()=='TScrollbar')
            self.assertFalse(scroll.winfo_ismapped())
    def test_last_detection_is_displayed_only_until_next_result(self):
        from types import SimpleNamespace
        from copy import deepcopy
        frame,data=self.camera();a=self.app;a.camera_all.set(True);now=time.monotonic()
        # Preview is fresh while the detector is processing its next frame.
        camera=a.camera;camera.observation=(frame,deepcopy(data),now-1.2);camera.preview_observation=(frame,{'detection_frame_at':now-1.2,'status':'processing','live_by_jig':{}},now)
        a.camera_overview_updated_at=-10;a.draw_camera_frame(now)
        self.assertIn('갱신 중',a.camera_jig_status.item('pallet','values')[1])
        self.assertEqual(a.camera_display_snapshot['result']['by_jig'],{})
        camera.observation=(frame,{'live_by_jig':{'pallet':{'selected':None,'candidates':[],'status':'not_found'}}},now+.1)
        camera.preview_observation=camera.observation;a.camera_overview_updated_at=-10;a.draw_camera_frame(now+.1)
        self.assertNotIn('갱신 중',a.camera_jig_status.item('pallet','values')[1])
    def test_stalled_or_disconnected_camera_never_keeps_old_outline(self):
        frame,data=self.camera();a=self.app;a.camera_all.set(True);now=time.monotonic()
        a.camera.observation=(frame,data,now-6);a.camera.preview_observation=(frame,{'detection_frame_at':now-6,'status':'processing'},now)
        a.draw_camera_frame(now);self.assertEqual(a.camera_display_snapshot['result']['by_jig'],{})
        a.draw_camera_frame(now+2);self.assertFalse(a.camera_canvas.find_withtag('image'))
