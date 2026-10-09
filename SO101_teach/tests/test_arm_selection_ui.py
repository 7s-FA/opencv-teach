import json
import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import ImageGrab

from so101_teach.domain import ROOT
from so101_teach.ui import App


class ArmSelectionUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        for name in ('profile.json','robot_profiles.json','jigs.json','workcell-preview.json','pi-connection.json'):
            shutil.copy2(ROOT/'data'/name,self.data/name)
        shutil.copytree(ROOT/'data/calibration',self.data/'calibration')
        shutil.copytree(ROOT/'data/episodes',self.data/'episodes')
        shutil.copy2(Path(__file__).parent/'fixtures/carrier-episode.json',self.data/'episodes/5c0668bca8994b4fa8f62418de78c229.json')
        # Switching tests start at arm2, independently of the operator's last
        # selected arm. Only this temporary workspace is changed.
        profiles=json.loads((self.data/'robot_profiles.json').read_text())
        initial=next(p for p in profiles.values() if p.get('robot_id')=='arm2')
        (self.data/'profile.json').write_text(json.dumps(initial))
        (self.data/'preferences.json').write_text(json.dumps({'device_host':'local'}))
        self.root=tk.Tk();self.app=App(self.root,self.data,render=True,auto_camera=False,auto_devices=False)
        self.root.geometry('1280x800');self.root.update()
    def tearDown(self):
        self.app.close();self.temp.cleanup()
    def choose(self,arm):
        index=next(i for i,k in enumerate(self.app.arm_profile_keys) if self.app.settings.library.items[k].get('robot_id')==arm)
        self.app.arm_selector.current(index);self.app.select_arm();self.root.update()

    def test_switch_filters_arm2_episodes_and_directs_new_arm3_to_its_calibration(self):
        app=self.app
        for profile in app.settings.library.items.values():
            if profile.get('robot_id')=='arm3':
                profile['calibration_status']='pending';profile['model_reference'].pop('angle_mapping_sha256',None)
                (self.data/profile['calibration_file']).with_suffix('.angles.json').unlink(missing_ok=True)
        for path in (self.data/'episodes').glob('*.json'):
            if json.loads(path.read_text()).get('robot_id')=='arm3':path.unlink()
        originals={p.name:json.loads(p.read_text()) for p in (self.data/'episodes').glob('*.json')}
        with patch('so101_teach.ui.MotionSession.start',side_effect=AssertionError('No motor motion')):
            self.choose('arm3')
            self.assertEqual(app.profile['robot_id'],'arm3');self.assertEqual(app.library_entries,[])
            self.assertEqual(app.settings.value('cal_port'),app.profile['port']);self.assertTrue(app.profile['port'].endswith('28218-if00'))
            self.assertEqual(app.settings.value('cal_role'),'팔로워');self.assertEqual(app.settings.tabs.select(),str(app.settings.pages['calibration']))
            with self.assertRaisesRegex(ValueError,'새 3점 보정'):app.connect_devices()
            with self.assertRaisesRegex(ValueError,'새 3점 보정'):app.motion_request('arm')
            with self.assertRaisesRegex(ValueError,'새 3점 보정'):app.commit_target()
            with self.assertRaisesRegex(ValueError,'초기 연결용 파일'):app.settings.start_calibration(True)
            self.assertEqual(app.episode['steps'],[])
            out=ROOT/'verification/dual-arm-select'
            for size in ((1280,800),(1180,760)):
                self.root.geometry(f'{size[0]}x{size[1]}');self.root.update()
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    self.root.update();time.sleep(.03)
                    if app.settings.calibration_panel.image is not None:break
                for _ in range(5):self.root.update();time.sleep(.03)
                x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
                image=ImageGrab.grab(bbox=(x,y,x+size[0],y+size[1]),xdisplay=os.environ['DISPLAY'])
                self.assertGreater(max(image.convert('L').getextrema()),100)
                image.save(out/f'arm3-calibration-{size[0]}x{size[1]}.png')
            self.choose('arm2')
            self.assertEqual(app.profile['robot_id'],'arm2');self.assertTrue(app.library_entries)
            self.assertTrue(all(doc.get('robot_id')=='arm2' for _,doc in app.library_entries))
            for name,doc in originals.items():
                after=json.loads((self.data/'episodes'/name).read_text())
                self.assertEqual(after['steps'],doc['steps']);self.assertEqual(after['jig_references'],doc['jig_references'])

    def test_held_or_moving_arm_and_calibration_cannot_be_switched(self):
        app=self.app;before=app.profile['robot_id'];session=SimpleNamespace(running=True,state='HOLD')
        app.session=session
        try:
            with self.assertRaisesRegex(ValueError,'토크 OFF'):self.choose('arm3')
            self.assertEqual(app.profile['robot_id'],before)
        finally:app.session=None
        app.settings.worker=SimpleNamespace(running=True)
        try:
            with self.assertRaisesRegex(ValueError,'보정'):self.choose('arm3')
            self.assertEqual(app.profile['robot_id'],before)
        finally:app.settings.worker=None

    def test_calibration_scene_and_restored_view_follow_the_selected_arm(self):
        import numpy as np
        import mujoco
        from so101_teach.domain import JOINTS
        from so101_teach.preview import configured_scene,DEFAULT_LOOKAT
        from so101_teach.workcell_preview import base_transform
        app=self.app;panel=app.settings.calibration_panel
        out=ROOT/'verification/calibration-arm-view';out.mkdir(parents=True,exist_ok=True)
        with patch('so101_teach.ui.MotionSession.start',side_effect=AssertionError('No motor access')):
            for arm in ('arm2','arm3'):
                self.choose(arm);app.show_page('settings');app.settings.tabs.select(app.settings.pages['calibration'])
                app.settings.vars['cal_role'].set('팔로워');panel.sync_role();panel.show('zero')
                spec,caption=panel.spec();q,view,config,_=spec
                self.assertIn('로봇팔'+arm[-1],caption);self.assertEqual(config['workcell']['active_arm_id'],arm)
                transform=base_transform(config['workcell']) if arm=='arm3' else np.eye(4)
                if arm=='arm3':
                    from so101_teach.domain import Calibration,ModelReference
                    other=next(p for p in app.settings.library.items.values() if p.get('robot_id')=='arm2')
                    from so101_teach.domain import EpisodeStore
                    entries=EpisodeStore(self.data/'episodes',Calibration(self.data/other['calibration_file']),'arm2').entries()
                    entries.sort(key=lambda row:row[1]['id']!=app.preferences.get('last_episode_id'))
                    episode=next(doc for _,doc in entries if any(s.get('safe_boundary')=='start' for s in doc['steps']))
                    safe=episode['steps'][0]['ticks']
                    expected=ModelReference(Calibration(self.data/other['calibration_file']),other['model_reference']).angles(safe)
                    np.testing.assert_allclose(config['workcell']['other_arm_joint_angles_rad'],expected)
                    self.assertEqual(view[1],-18.)
                np.testing.assert_allclose(view[3:],transform[:3,:3]@DEFAULT_LOOKAT+transform[:3,3]/1000)
                model=mujoco.MjModel.from_xml_string(configured_scene([],config['tcp'],workcell=config['workcell']))
                data=mujoco.MjData(model)
                for name,angle in zip(JOINTS,q):data.qpos[model.joint(name).qposadr[0]]=angle
                mujoco.mj_forward(model,data)
                np.testing.assert_allclose(data.body('base_link').xpos*1000,transform[:3,3],atol=1e-6)
                np.testing.assert_allclose(data.body('base_link').xmat.reshape(3,3),transform[:3,:3],atol=1e-6)
                inactive=data.site('arm3_preview_tcp').xpos.copy();active=data.site('active_tcp').xpos.copy()
                panel.show('positive');changed,_,changed_config,_=panel.spec()[0]
                self.assertEqual(changed_config['highlight_joint'],panel.name())
                for name,angle in zip(JOINTS,changed):data.qpos[model.joint(name).qposadr[0]]=angle
                mujoco.mj_forward(model,data)
                np.testing.assert_allclose(data.site('arm3_preview_tcp').xpos,inactive)
                self.assertGreater(np.linalg.norm(data.site('active_tcp').xpos-active),.01)
                panel.view=(55.,-30.,1.,0.,0.,0.);panel.sync_role()
                self.assertEqual(panel.view,(55.,-30.,1.,0.,0.,0.))
                panel.reset_view();self.assertEqual(panel.view,view);panel.show('zero')
                for width,height in ((1280,800),(1180,760)):
                    self.root.geometry(f'{width}x{height}');self.root.update();panel.image=None;panel.request=None
                    deadline=time.monotonic()+15
                    while panel.image is None and time.monotonic()<deadline:self.root.update();time.sleep(.03)
                    self.assertIsNotNone(panel.image,panel.caption.get());self.root.update()
                    x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
                    ImageGrab.grab(bbox=(x,y,x+width,y+height),xdisplay=os.environ['DISPLAY']).save(out/f'{arm}-{width}x{height}.png')
            app.settings.vars['cal_role'].set('리더');panel.sync_role()
            self.assertNotIn('workcell',panel.spec()[0][2]);self.assertIn('리더',panel.spec()[1])
            np.testing.assert_allclose(panel.view,(100.,-18.,.82,*DEFAULT_LOOKAT))
            self.choose('arm2');app.settings.vars['cal_role'].set('팔로워');panel.sync_role()
            self.assertEqual(panel.spec()[0][2]['workcell']['active_arm_id'],'arm2')

    def test_arm3_calibration_progress_path_is_sent_with_registered_port(self):
        from so101_teach.arm_workspace import progress_path
        self.choose('arm3');app=self.app;settings=app.settings
        self.assertEqual(progress_path(self.data,'follower',app.profile).name,'follower-arm3.json')
        fake=SimpleNamespace(running=False,start=lambda:None,close=lambda:None)
        with patch('so101_teach.calibration.CalibrationWorker',return_value=fake) as worker:
            settings.start_calibration()
        self.assertEqual(worker.call_args.args[0],app.profile['port'])
        self.assertEqual(worker.call_args.kwargs['progress_path'].name,'follower-arm3.json')

    def test_current_carrier_is_used_in_settings_and_teaching(self):
        from so101_teach.jig_compatibility import ASSEMBLED_CARRIER_SHA
        from so101_teach.inspection import read_triangles
        import hashlib
        app=self.app;settings=app.settings;key='b5ebdb54807c441c8267c15d77713b6a'
        out=ROOT/'verification/program-refresh';out.mkdir(parents=True,exist_ok=True)
        app.show_page('settings');settings.tabs.select(settings.pages['jigs'])
        settings.jig_choice.current(settings.jig_keys.index(key));settings.fill_jig()
        pane=settings.model_views['jigs'];deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self.root.update();time.sleep(.03)
            if pane.image is not None and pane.spec['stl']==app.catalog.items[key]['stl']:break
        self.assertIsNotNone(pane.image,pane.caption.get())
        self.assertEqual(hashlib.sha256(Path(pane.spec['stl']).read_bytes()).hexdigest(),ASSEMBLED_CARRIER_SHA)
        self.assertAlmostEqual(pane.meta['size_mm'][2],31.3,places=3)
        self.assertTrue(pane.meta['main_scene_assembly'])
        self.assertEqual(pane.spec['preview_jig']['mesh_yaw_offset_deg'],180.)
        self.assertEqual(pane.spec['preview_jig']['platform'],'TurtleBot3 Burger')
        self.assertEqual(len(read_triangles(pane.spec['stl'])),len(read_triangles(ROOT/'assets/carrier_assembly/carrier_assembly.stl')))
        for view in ('whole','detail'):
            pane.preset(view);pane.image=None;deadline=time.monotonic()+15
            while pane.image is None and time.monotonic()<deadline:self.root.update();time.sleep(.03)
            self.assertIsNotNone(pane.image,pane.caption.get());self.root.update()
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+1280,y+800),xdisplay=os.environ['DISPLAY']).save(out/f'carrier-settings-{view}.png')
        app.episode=json.loads((self.data/'episodes/5c0668bca8994b4fa8f62418de78c229.json').read_text())
        step=next(s for s in app.episode['steps'] if s.get('jig_id')==key)
        app.selected=step['id'];app.follow_jig.set(True);app.set_step_jig(key);app.apply_target(step['ticks']);app.refresh_steps();app.show_page('teach')
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self.root.update();time.sleep(.03)
            if app.last_render and any(j.get('id')==key and j.get('pose') for j in app.last_render[2]['jigs']):break
        shown=next(j for j in app.last_render[2]['jigs'] if j['id']==key)
        self.assertEqual(shown['stl'],pane.spec['stl']);self.assertEqual(shown['mesh_yaw_offset_deg'],180.)

    def test_jig_save_retains_installation_and_external_preview_file_refreshes(self):
        from copy import deepcopy
        from so101_teach.inspection import read_triangles,binary_stl
        app=self.app;settings=app.settings;key='b5ebdb54807c441c8267c15d77713b6a'
        app.show_page('settings');settings.tabs.select(settings.pages['jigs'])
        settings.jig_choice.current(settings.jig_keys.index(key));settings.fill_jig()
        installation=deepcopy(app.catalog.items[key]['physical_installation'])
        settings.vars['jig_name'].set('운반용 지그 이름 수정');settings.save_jig()
        self.assertEqual(app.catalog.items[key]['physical_installation'],installation)
        registered=(self.data/'jigs.json').read_bytes()
        path=self.data/'preview-only.stl';tri=read_triangles(ROOT/'assets/jigs/pallet.stl');path.write_bytes(binary_stl(tri))
        settings.vars['jig_stl'].set(str(path));pane=settings.model_views['jigs']
        def wait_height(height):
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                self.root.update();time.sleep(.03)
                if pane.meta and abs(pane.meta['size_mm'][2]-height)<.001:return
            self.fail('Updated STL did not reach the preview: '+pane.caption.get())
        wait_height(25.)
        tri[:,:,2]*=1.2;path.write_bytes(binary_stl(tri));wait_height(30.)
        self.assertEqual((self.data/'jigs.json').read_bytes(),registered)

    def test_read_only_connection_is_closed_before_switch(self):
        from so101_teach.motion import MotionSession
        from so101_teach.domain import Snapshot
        app=self.app;session=MotionSession(app.profile['port'],app.calibration);session.running=True;session.state='READ_ONLY'
        app.session=session;app.latest=Snapshot('follower',app.target.copy(),{n:{'torque':0} for n in app.target},time.monotonic(),time.time(),app.calibration.sha256,True,app.profile['port'])
        session.close=lambda:setattr(session,'running',False)
        self.choose('arm3');deadline=time.monotonic()+3
        while app.arm_switch_job and time.monotonic()<deadline:self.root.update();time.sleep(.03)
        self.assertIsNone(app.arm_switch_job);self.assertFalse(session.running)
        self.assertEqual(app.profile['robot_id'],'arm3');self.assertIsNone(app.session)

    def test_completed_calibration_enables_only_selected_arm_and_keeps_library_separate(self):
        self.choose('arm3');app=self.app;settings=app.settings
        arm2=next(p for p in settings.library.items.values() if p.get('robot_id')=='arm2')
        settings.calibration_role='follower'
        settings.worker=SimpleNamespace(running=False,port=app.profile['port'],destination=self.data/arm2['calibration_file'],close=lambda:None)
        settings.apply_completed_calibration()
        self.assertEqual(app.profile['robot_id'],'arm3');self.assertEqual(app.profile['calibration_status'],'ready')
        self.assertEqual(app.library_entries,[])
        with patch('so101_teach.ui.MotionSession.start') as start:
            app.connect_devices()
        start.assert_called_once();self.assertTrue(app.session.port.endswith('28218-if00'))


if __name__=='__main__':unittest.main()
