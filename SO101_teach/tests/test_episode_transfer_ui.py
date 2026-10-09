import json
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch
from tests import test_ui as fixtures
from so101_teach.domain import atomic_json


class EpisodeTransferUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def target(self):
        return {'arm':'arm2','revision':'revision','calibration_sha256':self.app.calibration.sha256,
                'episodes':{},'recipes':{'A':{'name':'기존 A 조립','timeout_seconds':900},'B':{'name':'기존 B 조립','timeout_seconds':600}},
                'settings':{'speed':400,'acquisition_seconds':5,'acquisition_attempts':3}}
    def ready(self):
        self.app.open_episode_adjust();self.root.update()
        a=self.app;panel=a.settings.episode_transfer_panel
        atomic_json(self.data/'pi-connection.json',{'host':'192.0.2.2','user':'tester','port':22,'identity_file':'','app_dir':'/tmp/Pi','python':'python3'})
        future=Future();future.set_result(self.target())
        with patch.object(a.settings.pool,'submit',return_value=future) as submit:
            a.export_episode_btn.invoke();panel.poll()
            self.assertEqual(submit.call_args.args[2],{'operation':'inspect','arm':'arm2'})
        self.root.update();return panel
    def test_export_button_opens_preview_without_motion_or_transmission(self):
        panel=self.ready()
        self.assertEqual(self.app.page,'teach')
        self.assertEqual(self.app.episode_adjust_panel.tabs.select(),str(panel.page))
        self.assertNotIn('export',self.app.settings.pages)
        self.assertIn('새로 저장',panel.current.get())
        self.assertEqual(panel.send_button.instate(['!disabled']),True)
        self.assertIsNone(self.app.session)
    def test_send_captures_selected_episode_without_execution_binding(self):
        a=self.app;a.commit_target();panel=self.ready()
        original=deepcopy(a.episode);future=Future()
        with patch.object(a.settings.pool,'submit',return_value=future) as submit:
            panel.send();request=submit.call_args.args[2]
            self.assertNotIn('slot',request);self.assertEqual(request['operation'],'store_episode');self.assertNotIn('execution_settings',request)
            self.assertEqual(request['episode']['steps'],original['steps'])
        self.assertEqual(a.episode,original)
        future.set_result({'arm':'arm2','slot':'B','name':original['name'],'step_count':len(original['steps']),'backup_directory':'/tmp/Pi/backup'})
        panel.poll();self.assertIn('전송 완료',panel.status.get());self.assertIsNone(panel.target)
        self.assertTrue(panel.send_button.instate(['disabled']))
    def test_changed_connection_or_calibration_prevents_send(self):
        a=self.app;a.commit_target();panel=self.ready()
        config=json.loads((self.data/'pi-connection.json').read_text());config['host']='192.0.2.3';atomic_json(self.data/'pi-connection.json',config)
        with self.assertRaisesRegex(ValueError,'주소 설정'):panel.send()
        panel=self.ready();panel.target['calibration_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'영점'):panel.send()
    def test_failure_and_small_window_remain_usable(self):
        panel=self.ready();future=Future();future.set_exception(ValueError('Pi 작업 실행 중'))
        panel.job=future;panel.job_kind='deliver';panel.poll()
        self.assertIn('작업 실행 중',panel.status.get());self.assertTrue(panel.query_button.instate(['!disabled']))
        from PIL import ImageGrab
        out=Path(__file__).parents[1]/'verification/episode-transfer';out.mkdir(exist_ok=True)
        for size in ('1180x760','1280x800'):
            self.root.geometry(size);self.root.update()
            self.assertLessEqual(panel.send_button.winfo_rootx()+panel.send_button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            self.assertTrue(panel.send_button.winfo_ismapped())
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/('export-'+size+'.png'))
            self.app.show_page('teach');self.root.update()
            self.assertGreater(self.app.steps.winfo_height(),40)
            self.assertLessEqual(self.app.capture_btn.winfo_rooty()+self.app.capture_btn.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/('teach-'+size+'.png'))
            self.app.show_page('settings');self.root.update()
        self.app.show_page('teach');self.root.update()
        self.assertTrue(self.app.export_episode_btn.winfo_ismapped())
        self.assertGreater(self.app.steps.winfo_height(),40)
        self.assertLessEqual(self.app.capture_btn.winfo_rooty()+self.app.capture_btn.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/'teach.png')
