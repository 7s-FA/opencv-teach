import json,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import ImageGrab
from tests import test_ui as ui_fixture
from tests.test_failure_archive import fixture
from so101_teach.domain import ROOT


class FailureHistoryUITests(unittest.TestCase):
    setUp=ui_fixture.UITests.setUp
    tearDown=ui_fixture.UITests.tearDown
    def wait(self,predicate):
        until=time.monotonic()+5
        while not predicate() and time.monotonic()<until:self.root.update();time.sleep(.015)
        self.assertTrue(predicate())
    def show(self):
        self.root.geometry('1920x1043');self.app.camera_tabs.select(self.app.camera_errors);self.app.show_page('camera');self.root.update();return self.app.camera_errors
    def test_record_image_logs_filter_copy_zoom_and_maximized_layout(self):
        key=fixture(self.data,ROOT/'tests/fixtures/carrier-two-poses-20261012/observed-02.jpg');p=self.show();self.wait(lambda:p.payload is not None)
        self.assertEqual(p.payload['key'],key);self.assertIsNotNone(p.photo);self.assertIn('안착 불합격',p.log.get('1.0','end'))
        self.assertIn('기록 저장보다',p.image_info.get())
        age=p.payload['images'][0]['age'];p.payload['images'][0]['age']=None;p.select_image()
        self.assertIn('촬영 미확인',p.image_info.get())
        p.payload['images'][0]['age']=age;p.select_image()
        p.scale(1.25);self.assertIsNotNone(p.zoom);p.fit();self.assertIsNone(p.zoom)
        p.copy_log();self.assertIn('안착 불합격',self.root.clipboard_get())
        self.assertGreater(p.canvas.winfo_width(),700);self.assertGreater(p.canvas.winfo_height(),300)
        self.assertLessEqual(p.copy_button.winfo_rootx()+p.copy_button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        self.assertLessEqual(p.log.winfo_rooty()+p.log.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        out=ROOT/'verification/failure-history-ui-20261012';out.mkdir(parents=True,exist_ok=True);self.root.update()
        ImageGrab.grab((self.root.winfo_rootx(),self.root.winfo_rooty(),self.root.winfo_rootx()+self.root.winfo_width(),self.root.winfo_rooty()+self.root.winfo_height())).save(out/'error-record-tab.png')
        p.arm.set('팔3');p.filter_rows();self.wait(lambda:p.payload is not None and p.payload['key'].startswith('run:'))
        self.assertIsNone(p.image);self.assertIn('저장된 이미지가 없습니다',p.image_info.get())
        p.search.set('없는 오류');self.root.update();self.assertFalse(p.table.get_children());self.assertIsNone(p.payload)
    def test_history_navigation_does_not_start_camera_and_empty_state_is_clear(self):
        self.app.camera_auto_view=True
        with patch.object(self.app,'start_camera') as start:
            p=self.show();self.wait(lambda:p.loaded);self.assertFalse(self.app.camera_needed());start.assert_not_called()
        self.assertIn('저장된 오류 기록이 없습니다',p.status.get());self.assertTrue(p.copy_button.instate(['disabled']))

    def test_inspection_timeline_is_visible_and_included_in_copied_log(self):
        from tests.test_failure_timeline import fixture as timeline_fixture
        p=self.show();self.wait(lambda:p.loaded)
        value={**timeline_fixture(),'key':'run:r','images':[],'warnings':[]}
        p.rows=[{'key':'run:r','arm':'arm3','at':106.,'command':'load_b'}]
        p.display_record(value);self.root.update()
        self.assertTrue(p.timeline_frame.winfo_ismapped())
        self.assertIn('놓기 위치',p.timeline_vars['검사 지정 스텝'].get())
        self.assertIn('복귀 위치',p.timeline_vars['중단 시 진행 스텝'].get())
        self.assertIn('놓고 들기',p.timeline_vars['마지막 완료 스텝'].get())
        p.copy_log();text=self.root.clipboard_get()
        for title in ('검사 지정 스텝','중단 시 진행 스텝','마지막 완료 스텝','검사 시작','판정 완료','검사 소요'):
            self.assertIn(title+':',text)
        self.assertIn('3.190초',text);self.assertIn('정지 확정 상태 미기록',text)
        p.clear_detail();self.root.update();self.assertFalse(p.timeline_frame.winfo_ismapped())
    def test_failed_fetch_keeps_ui_responsive_and_refresh_recovers(self):
        from so101_teach import failure_archive_client
        real=failure_archive_client.fetch
        with patch.object(failure_archive_client,'fetch',side_effect=ValueError('Pi 연결 실패')):
            p=self.show();self.wait(lambda:'Pi 연결 실패' in p.status.get())
        fixture(self.data);p.refresh();self.wait(lambda:p.payload is not None)
        self.assertIsNotNone(p.image)

    def test_late_record_response_cannot_replace_a_newer_selection(self):
        import threading
        from so101_teach import failure_archive_client
        key=fixture(self.data);started=threading.Event();release=threading.Event();real=failure_archive_client.fetch
        def fetch(data,source,message,config):
            if message.get('key')==key:started.set();release.wait(3)
            return real(data,source,message,config)
        with patch.object(failure_archive_client,'fetch',side_effect=fetch):
            p=self.show();self.wait(started.is_set)
            other='run:'+'b'*32;p.table.selection_set(other);self.root.update();release.set()
            self.wait(lambda:p.payload is not None and p.payload['key']==other)
        self.assertIsNone(p.image);self.assertIn('카메라 연결 오류',p.log.get('1.0','end'))

    def test_paired_raw_and_decision_images_are_selectable_in_maximized_history(self):
        import base64
        from integration.failure_images import FailureImages
        evidence=ROOT/'verification/tcp_goal_anchor_20261013/image_preview'
        doc=json.loads((evidence/'source_metadata.json').read_text());at=time.monotonic()-.2
        inspection=doc['inspection'];inspection['result']['at']=at
        cache=FailureImages(self.data);cache.remember({'preview_at':at,'preview_image':base64.b64encode((evidence/'source.jpg').read_bytes()).decode()})
        context={**doc['context'],'run_id':'f'*32,'inspection_log':None}
        runs=self.data/'episode-cli-runs';runs.mkdir(exist_ok=True)
        (runs/('f'*32+'.json')).write_text(json.dumps({'run_id':'f'*32,'arm':'arm3','events':[{'at':time.time(),'status':'B_FAILED:검증용 이미지 표시'}]}))
        path=cache.save('검증용 · '+doc['error'],context=context,inspection=inspection)
        panel=self.show();self.root.geometry('1920x1080');self.wait(lambda:panel.payload is not None)
        self.assertEqual(panel.payload['key'],'evidence:'+path.parent.name)
        self.assertEqual(panel.image_labels,['검사 원본','검사 판정 표시'])
        panel.image_name.set('검사 판정 표시');panel.select_image();self.root.update()
        self.assertIsNotNone(panel.image);self.assertIn('동일 원본 프레임',panel.image_info.get())
        out=ROOT/'verification/tcp_goal_anchor_20261013';ImageGrab.grab().save(out/'failure-pair-ui.png')
