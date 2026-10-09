import concurrent.futures
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from tests.test_ui import UITests as _Fixture

DETAILS=[{'name':'D435','device':'/dev/video4','source':'/dev/v4l/by-id/d435-rgb','kind':'color'}, {'name':'Innomaker','device':'/dev/video6','source':'/dev/v4l/by-id/innomaker','kind':'color'}]

class CameraPickerTests(unittest.TestCase):
    setUp=_Fixture.setUp
    tearDown=_Fixture.tearDown
    def test_remote_refresh_preserves_current_camera_and_applies_selected_source(self):
        a=self.app;s=a.settings;a.remote_mode=True
        a.remote=SimpleNamespace(http=Mock(return_value={'devices':{'camera_details':DETAILS}}))
        before=dict(a.profile['camera']);source=s.camera_source()
        with patch('so101_teach.camera_inventory.camera_inventory',side_effect=AssertionError('PC scan forbidden')):
            s.refresh_camera_devices();s.camera_devices_job[0].result(3);s.poll_camera_devices()
        self.assertEqual(a.profile['camera'],before);self.assertEqual(s.camera_source(),source)
        values=s.camera_source_choice['values'];self.assertIn('Pi · D435 · 컬러 (/dev/video4)',values)
        s.vars['cam_source'].set(values[0])
        with patch.object(a,'persist_configuration'),patch.object(a,'suspend_camera'),patch.object(a,'invalidate_jig_measurements'):
            s.save_camera_device()
        self.assertEqual(a.profile['camera']['source'],DETAILS[0]['source'])
        a.remote=None;a.remote_mode=False
    def test_stale_refresh_and_manual_path(self):
        s=self.app.settings;s.vars['cam_source'].set('/dev/video20');s.set_camera_choices(DETAILS)
        self.assertEqual(s.camera_source(),'/dev/video20')
        future=concurrent.futures.Future();future.set_result(DETAILS)
        s.camera_devices_job=(future,True,object());s.poll_camera_devices()
        self.assertTrue(all(label.startswith('PC') for label in s.camera_choices))
    def test_picker_layout(self):
        a=self.app;s=a.settings;a.show_page('settings');s.tabs.select(s.pages['camera']);s.set_camera_choices(DETAILS);self.root.update()
        self.assertGreater(s.camera_source_choice.winfo_width(),300)
        self.assertLess(s.camera_refresh_btn.winfo_rootx()+s.camera_refresh_btn.winfo_width(),self.root.winfo_rootx()+1280)
        s.camera_source_choice.event_generate('<Button-1>');self.root.update()
        from PIL import ImageGrab
        ImageGrab.grab().save('/tmp/so101-camera-picker.png')

