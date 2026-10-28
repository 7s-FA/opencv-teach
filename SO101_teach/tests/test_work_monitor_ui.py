import queue,time,threading,unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image
from tests.test_ui import UITests as Fixture
from tests.test_work_monitor_model import fixture

class FakeObserver:
    def __init__(self,*args):self.stop=threading.Event();self.results=queue.Queue()
    def start(self):pass
    def close(self):self.stop.set()

class MonitorUITests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    def prepare(self):
        a=self.app;p=a.camera_work_monitor;p.factory=FakeObserver
        a.camera_tabs.select(p);a.show_page('camera');self.root.update();return p
    def sample(self):
        now=time.monotonic();s=fixture()
        return {**s,'connected':True,'received':now,'at':time.time(),'camera':{'running':True,'arm':'arm2','frozen':{}},
                'frame':Image.new('RGB',(1280,720),'#8899aa'),'frame_key':('camera',1),'frame_received':now,'frame_age':.01}
    def test_select_and_leave_monitor_never_starts_stops_or_reconfigures_devices(self):
        import traceback
        traces=[]
        a=self.app;p=a.camera_work_monitor;p.factory=FakeObserver
        camera=SimpleNamespace(running=False,close=Mock(),set_mode=Mock(side_effect=lambda **kw:traces.append(''.join(traceback.format_stack(limit=9)))),observation=None,preview_observation=None,error=None)
        a.camera=camera
        with patch.object(a,'start_camera',side_effect=AssertionError('No camera start')),patch.object(a,'suspend_camera',side_effect=AssertionError('No camera stop')):
            a.camera_tabs.select(p);a.show_page('camera');self.root.update();a.update_camera_mode()
            observer=p.observer;self.assertIsNotNone(observer);self.assertFalse(a.camera_needed())
            camera.set_mode.assert_not_called()
            a.camera_tabs.select(0);self.root.update()
            self.assertTrue(observer.stop.is_set());self.assertEqual(camera.set_mode.call_count,0,'\n'.join(traces));camera.close.assert_not_called()
    def test_external_record_and_camera_render_without_local_execution(self):
        p=self.prepare();p.consume(self.sample());self.root.update()
        self.assertIn('BUILD_B',p.task.get());self.assertIn('중단 잡기 위치',p.progress.get())
        self.assertEqual(p.checks.item('0','values')[1],'검사 중');self.assertTrue(p.canvas.find_withtag('frame'))
        self.assertIsNone(self.app.session);self.assertIsNone(self.app.remote)
    def test_same_snapshot_does_not_rewrite_log_or_image(self):
        p=self.prepare();sample=self.sample();p.consume(sample);self.root.update()
        with patch.object(p.log,'delete',wraps=p.log.delete) as log,patch.object(p.canvas,'itemconfigure',wraps=p.canvas.itemconfigure) as picture:
            p.consume(sample);p.consume(sample)
        log.assert_not_called();picture.assert_not_called()
    def test_disconnect_preserves_records_but_removes_live_image(self):
        p=self.prepare();p.consume(self.sample());before=p.log.get('1.0','end')
        p.consume({'connected':False,'error':'network'});self.root.update()
        self.assertEqual(p.log.get('1.0','end'),before);self.assertFalse(p.canvas.find_withtag('frame'))
        self.assertIn('마지막 확인',p.task.get());self.assertIn('연결 끊김',p.connection.get())
    def test_stale_camera_cannot_remain_labelled_live(self):
        p=self.prepare();sample=self.sample();sample['frame_received']-=10;p.consume(sample)
        self.assertFalse(p.canvas.find_withtag('frame'));self.assertIn('영상 대기',p.camera_status.get())
    def test_filters_raw_view_and_history_keep_original_event_text(self):
        p=self.prepare();sample=self.sample()
        sample['events'].append(dict(at=14.,arm='arm2',run_id=sample['runs'][0]['run_id'],sequence=5,status='B_INSPECTION_PASS:step'))
        p.consume(sample);p.category.set('검사');p.render_records()
        text=p.log.get('1.0','end');self.assertIn('검사 통과',text);self.assertNotIn('지그 채택',text)
        p.raw.set(True);p.render_records();self.assertIn('B_INSPECTION_STARTED:step:하단 확인',p.log.get('1.0','end'))
    def test_maximized_video_records_and_controls_stay_inside_view(self):
        p=self.prepare();p.consume(self.sample());self.root.update()
        self.assertGreater(p.canvas.winfo_width(),600);self.assertGreater(p.canvas.winfo_height(),250)
        self.assertGreater(p.log.winfo_height(),180)
        for widget in (p.canvas,p.checks,p.log,p.connect_button,p.history_choice):
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
    def test_panel_close_only_stops_observer(self):
        p=self.prepare();observer=p.observer
        with patch.object(self.app,'motion_request',side_effect=AssertionError('No motion')),patch.object(self.app,'stop_camera',side_effect=AssertionError('No camera stop')):
            p.close();self.assertTrue(observer.stop.is_set())

    def test_monitor_keeps_shared_footer_visible_and_connection_gated(self):
        self.prepare();a=self.app;footer=a.message_status.master
        self.assertTrue(footer.winfo_ismapped())
        for button in (a.arm_btn,a.hold_btn,a.release_btn):
            self.assertTrue(button.winfo_ismapped())
            self.assertTrue(button.instate(['disabled']))
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        a.show_page('teach');self.root.update_idletasks()
        self.assertTrue(footer.winfo_ismapped())

    def test_default_log_matches_terminal_and_can_select_one_job(self):
        p=self.prepare();sample=self.sample();sample['events'].append(dict(at=18.,arm='arm3',run_id='c'*32,sequence=1,status='IDLE_TORQUE_RELEASE_REQUESTED'))
        p.consume(sample)
        self.assertEqual(p.history.get(),'최근 전체 기록')
        self.assertIn('팔3 | 새 명령 없음 · 3초 정지 후 토크 해제 요청',p.log.get('1.0','end'))
        p.history.set('현재 / 최근 작업');p.render_records()
        self.assertNotIn('토크 해제 요청',p.log.get('1.0','end'))
        p.history.set('최근 전체 기록');p.render_records();p.toggle_raw()
        self.assertIn('IDLE_TORQUE_RELEASE_REQUESTED',p.log.get('1.0','end'))
