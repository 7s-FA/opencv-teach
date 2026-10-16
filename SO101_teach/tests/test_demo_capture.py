import time,unittest,json
from PIL import ImageGrab
from tests.test_demo_workspace import DemoWorkspaceTests
from so101_teach.domain import ROOT

class DemoCaptureTests(unittest.TestCase):
    setUp=DemoWorkspaceTests.setUp
    tearDown=DemoWorkspaceTests.tearDown
    wait_for=DemoWorkspaceTests.wait_for
    def test_production_demo_3d_and_capture_scenes(self):
        a=self.app;r=a.root;out=ROOT/'verification/demo-refresh';r.geometry('1920x1080');r.update()
        a.render_enabled=True;a.workcell_view();self.wait_for(lambda:a.last_rgb is not None,20)
        def capture(name):
            r.update_idletasks();x,y=r.winfo_rootx(),r.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+1920,y+1080)).save(out/(name+'.png'))
        capture('teaching')
        a.show_page('devices');r.update();capture('devices')
        a.show_page('camera');p=a.camera_inspection;a.camera_tabs.select(p)
        self.wait_for(lambda:p.worker.result and len(p.worker.result['rows'])==9 and all(row.get('state')!='checking' for row in p.worker.result['rows']),12)
        a.draw_camera_frame(time.monotonic());capture('inspection-complete')
        self.assertTrue(p.capture_snapshot['metadata']['demo_replay'])
        a.root.event_generate('<F6>');r.update();self.assertEqual(a.demo_scene,'faults')
        self.wait_for(lambda:p.worker.result and p.worker.result['key']==p.context and len(p.worker.result['rows'])==9 and all(row.get('state')!='checking' for row in p.worker.result['rows']) and any(row.get('quality')=='abnormal' for row in p.worker.result['rows']),12)
        a.draw_camera_frame(time.monotonic());capture('inspection-faults')
        a.root.event_generate('<F8>');r.update()
        self.wait_for(lambda:len(list((a.data_dir/'captures'/'앱 화면').glob('*.png')))==1)
        a.root.event_generate('<F7>');r.update();self.wait_for(lambda:p.capture_snapshot is None,4);capture('video-lost')
        a.root.event_generate('<F7>');r.update();self.wait_for(lambda:p.capture_snapshot is not None,6)
        a.show_page('settings');a.settings.tabs.select(a.settings.pages['tcp']);r.update()
        self.wait_for(lambda:a.settings.model_views['tcp'].image is not None,15);capture('settings-tcp')
        self.assertTrue(all(app.demo_mode for app in self.manager.apps.values()))
