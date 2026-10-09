import json,time,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from PIL import ImageGrab
import cv2
from . import test_ui as fixtures
from so101_teach.vision_service import MultiDetector
from so101_teach.domain import ROOT

class GridDetectionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_grid_mode_can_be_saved_and_crossings_render_without_changing_teaching(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-grid';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'current.png'))
        key='b5ebdb54807c441c8267c15d77713b6a';a.profile=cfg['profile'];a.catalog.items=deepcopy(cfg['jigs']);a.catalog.revision+=1
        for item in a.catalog.items.values():item['stl']=str(ROOT/'data/jig_assets'/Path(item['stl']).name)
        before=deepcopy(a.episode);a.show_page('settings');s=a.settings;s.tabs.select(s.pages['jigs']);s.refresh_jigs();s.jig_choice.current(s.jig_keys.index(key));s.fill_jig()
        s.vars['jig_method'].set('외곽+교차점');s.save_jig();self.assertEqual(a.catalog.items[key]['method'],'grid');self.assertEqual(a.episode,before)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update()
            canvas=s.scroll_canvases[str(s.pages['jigs'])];canvas.yview_moveto(1);self.root.update()
            self.assertLessEqual(s.jig_detection_note.winfo_rootx()+s.jig_detection_note.winfo_width(),canvas.winfo_rootx()+canvas.winfo_width())
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/grid-settings-{width}x{height}.png')
        data=MultiDetector(a.catalog,a.profile).process(frame);self.assertTrue(data['live_by_jig'][key]['selected']['grid_verified'])
        data['preview_detection_age_s']=.8
        for value in data['live_by_jig'].values():value['preview_detection_age_s']=.8
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.camera_sleeping=False;a.camera_view_requested=True
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]])
        a.show_page('camera');a.camera_jig_choice.current(0);a.select_camera_jig();self.root.update();self.root.after_cancel(a.job)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update();a.camera.observation=(frame,data,time.monotonic())
            a.poll();self.root.after_cancel(a.job);self.root.update();a.draw_camera_frame(time.monotonic());self.root.update()
            self.assertEqual(a.camera_jig_status.item(key,'values')[1],'교차점 · 0.8초 전')
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/grid-camera-{width}x{height}.png')
        self.assertEqual(a.episode,before)
