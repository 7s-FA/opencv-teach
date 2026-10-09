import unittest,time,tkinter as tk
from copy import deepcopy
from . import test_ui as ui_fixtures
from . import test_camera_overview as camera_fixtures
from so101_teach.ui import App
from so101_teach.domain import read_json

class AdoptionDisplayUITests(unittest.TestCase):
    setUp=ui_fixtures.UITests.setUp
    tearDown=ui_fixtures.UITests.tearDown
    camera=camera_fixtures.CameraOverviewTests.camera
    def test_toggle_is_display_only_and_saved_on_restart(self):
        frame,data=self.camera();a=self.app;revision=a.catalog.revision;generation=a.detector.generation;before=deepcopy(data)
        self.assertFalse(a.show_adoption.get());self.assertNotIn('adoption',a.camera_jig_status['displaycolumns'])
        a.show_adoption.set(True);a.toggle_adoption_display();self.assertIn('adoption',a.camera_jig_status['displaycolumns'])
        self.assertEqual(data,before);self.assertEqual(a.catalog.revision,revision);self.assertEqual(a.detector.generation,generation)
        self.assertTrue(read_json(self.data/'preferences.json')['camera_show_adoption'])
        a.camera.running=False;a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertTrue(self.app.show_adoption.get());self.assertIn('adoption',self.app.camera_jig_status['displaycolumns'])
        self.app.show_adoption.set(False);self.app.toggle_adoption_display();self.assertNotIn('adoption',self.app.camera_jig_status['displaycolumns'])
    def test_live_detection_confirmation_and_held_missing_are_independent(self):
        frame,data=self.camera();a=self.app;now=time.monotonic();data=deepcopy(data)
        data['by_jig']['pallet']={'selected':None,'stable_candidate_seconds':.2,'stable_candidate_required_seconds':.6}
        a.camera.observation=(frame,data,now);a.show_adoption.set(True);a.toggle_adoption_display()
        row=a.camera_jig_status.item('pallet','values');self.assertEqual(row[1],'돌출부 확인');self.assertEqual(row[2],'관측 확인 중')
        data['by_jig']['pallet']={'selected':{'metric':{}},'pose_held':True,'pose_measured_at':now-2}
        data['live_by_jig']['pallet']={'selected':None,'candidates':[],'status':'not_found'}
        a.camera_overview_updated_at-=1.1;a.draw_camera_frame(time.monotonic());row=a.camera_jig_status.item('pallet','values');self.assertEqual(row[1],'미검출');self.assertIn('채택 유지',row[2])
        a.camera.observation=(frame,data,now-3);a.update_camera_overview({});self.assertIn('채택 유지',a.camera_jig_status.item('pallet','values')[2]);self.assertEqual(a.camera_results(),{})
        data['by_jig']['pallet']['pose_measured_at']=now-11;a.update_camera_overview({});self.assertEqual(a.camera_jig_status.item('pallet','values')[2],'미채택')
    def test_single_and_overview_layouts_show_and_hide_status(self):
        from PIL import ImageGrab
        frame,data=self.camera();a=self.app;a.show_adoption.set(True);a.toggle_adoption_display()
        def shot(name):
            self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/adoption-'+name+'.png')
        for size in ('1920x1043',):
            self.root.geometry(size);a.camera.observation=(frame,data,time.monotonic());self.root.update();a.draw_camera_frame(time.monotonic());shot('all-'+size)
            self.assertLessEqual(a.camera_jig_status.winfo_rootx()+a.camera_jig_status.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            self.assertGreater(a.camera_canvas.winfo_width(),350)
        self.root.geometry('1280x800');a.camera_jig_choice.current(1);a.select_camera_jig();self.root.update();shot('single')
        self.assertTrue(a.camera_adoption_label.winfo_ismapped());self.assertIn('채택됨',a.camera_adoption_text.get())
        a.show_adoption.set(False);a.toggle_adoption_display();self.root.update();self.assertFalse(a.camera_adoption_label.winfo_ismapped());shot('off')
