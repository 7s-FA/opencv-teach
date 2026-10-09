import json,tempfile,unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock,patch
import tkinter as tk
from . import test_ui as fixtures
from so101_teach.ui import App
from so101_teach.domain import atomic_json,read_json
from so101_teach.remote_config import configuration_bundle,materialize
from so101_teach.configuration import JigCatalog

class FloorHeightUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def shot(self,name):
        from PIL import ImageGrab
        self.root.update();x=self.root.winfo_rootx();y=self.root.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/floor-height-'+name+'.png')
    def test_blank_save_new_copy_and_floor_labels(self):
        a=self.app;s=a.settings;a.show_page('settings');s.tabs.select(s.pages['jigs']);self.root.update()
        self.assertEqual(s.vars['jig_z'].get(),'0');self.shot('zero')
        s.vars['jig_z'].set('');s.save_jig();self.assertEqual(s.vars['jig_z'].get(),'0')
        self.assertEqual(read_json(self.data/'jigs.json')['pallet']['support_height_mm'],0)
        s.vars['jig_z'].set('5');s.save_jig();s.copy_jig();self.assertEqual(s.vars['jig_z'].get(),'5');self.shot('five')
        s.new_jig();self.assertEqual(s.vars['jig_z'].get(),'0')
    def test_model_zero_and_height_display_do_not_change_ticks(self):
        a=self.app;before=a.kin.fk(a.target).copy();ticks=a.target.copy();a.commit_target()
        self.assertEqual(a.table_z.get(),'5');a.show_page('model');self.shot('model')
        a.table_z.set('10');a.save_model();self.assertAlmostEqual(a.profile['table_z_mm'],-12.4)
        self.assertAlmostEqual(a.catalog.floor_profile['table_z_mm'],-12.4);self.assertEqual(a.target,ticks);self.assertEqual(a.episode['steps'][0]['ticks'],ticks)
        self.assertAlmostEqual(read_json(self.data/'profile.json')['table_z_mm'],-12.4)
        import numpy as np
        np.testing.assert_allclose(a.kin.fk(ticks),before)
        a.reset_model();self.assertEqual(a.table_z.get(),'0')
    def test_selected_profile_and_restart_keep_saved_arm_height(self):
        a=self.app;original=deepcopy(a.profile)
        key=a.settings.library.save(a.profile['name'],a.profile);a.settings.active_profile_key=key
        a.table_z.set('10');a.save_model()
        self.assertAlmostEqual(read_json(self.data/'robot_profiles.json')[key]['table_z_mm'],-12.4)
        a.install_profile(original);self.assertEqual(a.table_z.get(),'10')
        a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.table_z.get(),'10');self.assertAlmostEqual(self.app.profile['table_z_mm'],-12.4)
    def test_pi_receives_absolute_plane_before_local_save_and_rejection_preserves_state(self):
        from types import SimpleNamespace
        a=self.app;before=deepcopy(a.profile);a.remote_mode=True;a.remote=SimpleNamespace(error=None,sync_bundle=Mock(side_effect=ValueError('Pi rejected')))
        try:
            a.table_z.set('10')
            with self.assertRaises(ValueError):a.save_model()
            self.assertEqual(a.profile,before);self.assertFalse(a.adjustments_path.exists())
            a.remote.sync_bundle.side_effect=None;a.save_model()
            self.assertAlmostEqual(a.remote.sync_bundle.call_args.args[0]['profile']['table_z_mm'],-12.4)
            self.assertAlmostEqual(a.profile['table_z_mm'],-12.4)
        finally:a.remote_mode=False;a.remote=None
    def test_adjusted_floor_is_loaded_before_legacy_catalog_migration(self):
        a=self.app;item=deepcopy(a.catalog.items['pallet']);item.pop('support_height_mm');item['support_z_mm']=0
        a.close();atomic_json(self.data/'jigs.json',{'pallet':item});atomic_json(self.data/'model-adjustments.json',{'calibration_sha256':a.calibration.sha256,'trim_ticks':dict(a.reference.trims),'table_z_mm':-5,'hold_seconds':10})
        self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.catalog.items['pallet']['support_height_mm'],5)
        self.assertEqual(read_json(self.data/'jigs.json')['pallet']['support_height_mm'],5)
    def test_checker_height_is_converted_once_and_keeps_xy(self):
        s=self.app.settings;s.vars['origin_X'].set('10');s.vars['origin_Y'].set('20')
        for h,z in [('0',-7.4),('5',-2.4)]:
            s.vars['origin_Z'].set(h)
            with patch.object(s,'run_job',side_effect=lambda run,done:run()),patch('so101_teach.camera_calibration.board_extrinsics',return_value={}) as board:
                s.save_extrinsics_frame(None)
            xyz=board.call_args.args[2];self.assertEqual(xyz[:2],[10,20]);self.assertAlmostEqual(xyz[2],z)
    def test_render_and_remote_bundle_use_same_relative_height(self):
        a=self.app;a.catalog.items['pallet']['support_height_mm']=5
        a.renderer=Mock();a.renderer.submit.return_value=True;a.renderer.poll.return_value=None;a.render_enabled=True
        self.root.after_cancel(a.render_job);a.render_tick()
        request=a.renderer.submit.call_args.args;self.assertAlmostEqual(request[2]['jigs'][0]['bottom_z_mm'],-2.4)
        z=a.kin.fk(a.target)[2,3]-a.profile['table_z_mm'];self.assertIn(f'바닥↑Z {z:.1f}',a.tcp_label.get())
        with tempfile.TemporaryDirectory() as d:
            materialize(d,configuration_bundle(a));c=JigCatalog(d)
            self.assertEqual(c.items['pallet']['support_height_mm'],5);self.assertNotIn('support_z_mm',c.items['pallet'])
        a.render_enabled=False;a.renderer=None
    def test_saved_scene_with_zero_and_five_mm_and_rendered_model(self):
        import cv2,time
        from types import SimpleNamespace
        from so101_teach.vision_service import MultiDetector
        from so101_teach.height_reference import support_plane_profile,detection_plane_z
        from so101_teach.vision import projected_jig_axes
        import numpy as np
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-lens';cfg=read_json(folder/'fixture.json');frame=cv2.imread(str(folder/'input.jpg'))
        a.profile=cfg['profile'];a.catalog.items=cfg['jigs']
        for key,item in a.catalog.items.items():item.pop('support_z_mm',None);item['support_height_mm']=0 if key=='pallet' else 5
        a.catalog.revision+=1;a.detector=MultiDetector(a.catalog,a.profile);result=a.detector.process(frame)
        for key,item in a.catalog.items.items():
            chosen=result['live_by_jig'][key]['selected'];self.assertIsNotNone(chosen,key)
            plane=support_plane_profile(a.profile,item);axes=projected_jig_axes(chosen,plane,detection_plane_z(plane,a.catalog.mesh(key)))
            np.testing.assert_allclose(chosen['axes_px'],axes,atol=1e-5)
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]])
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,result,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.show_page('camera');a.select_camera_jig(list(a.catalog.items)[1]);self.root.update();a.draw_camera_frame(time.monotonic());self.shot('camera')
        self.assertIn('받침 높이 5 mm (바닥=0)',a.camera_profile_text.get())
        a.camera.running=False;a.render_enabled=True;a.show_page('model');a.last_render=None
        end=time.monotonic()+15
        while a.last_rgb is None and time.monotonic()<end:self.root.update();time.sleep(.05)
        self.assertIsNotNone(a.last_rgb);self.shot('rendered')
