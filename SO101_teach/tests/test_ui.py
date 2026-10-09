from tests.fixtures import DATA
import unittest,tempfile,shutil,time
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from so101_teach.domain import ROOT,JOINTS,Snapshot,read_json
from so101_teach.ui import App

class UITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        shutil.copy2(DATA/'profile.json',self.data/'profile.json');shutil.copytree(DATA/'calibration',self.data/'calibration')
        self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False);self.root.geometry('1280x800');self.root.update()
    def tearDown(self):
        from types import SimpleNamespace
        for device in (self.app.camera,self.app.session,self.app.leader_session,self.app.settings.worker):
            if isinstance(device,SimpleNamespace):device.running=False
        self.app.close();self.temp.cleanup()
    def test_gripper_slider_saves_integer_ticks_without_device_connection(self):
        a=self.app
        with patch('so101_teach.ui.MotionSession.start',side_effect=AssertionError('No motor connection')):
            a.slider_tick('gripper','888');a.step_name.set('집게 888틱');a.commit_target();a.play();a.stop_preview()
        path=next((self.data/'episodes').glob('*.json'));doc=read_json(path)
        self.assertEqual(doc['steps'][0]['ticks']['gripper'],888)
        self.assertEqual(doc['unit'],'motor_ticks');self.assertEqual(doc['steps'][0]['source']['kind'],'edited')
    def test_real_capture_is_distinguished_from_edited_goal(self):
        a=self.app;ticks=a.reference.middle.copy();ticks['gripper']=900
        a.latest=Snapshot('follower',ticks,{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        a.capture();self.assertEqual(a.episode['steps'][0]['source']['kind'],'measured')
        self.assertEqual(a.episode['steps'][0]['ticks']['gripper'],900)
    def test_navigation_keeps_episode_and_selected_step(self):
        a=self.app;a.commit_target();selected=a.selected;eid=a.episode['id'];a.show_page('camera');a.show_page('devices');a.show_page('teach')
        self.assertEqual(a.selected,selected);self.assertEqual(a.episode['id'],eid);self.assertIn('●',a.nav_buttons['teach']['text'])
    def test_reorder_rename_and_persistence(self):
        a=self.app;a.commit_target();first=a.selected;a.selected=None;a.step_name.set('두 번째');a.commit_target();second=a.selected
        a.move_step(-1);self.assertEqual(a.episode['steps'][0]['id'],second)
        a.episode_name.set('검사 에피소드');p=a.save_episode();d=a.store.load(p)
        self.assertEqual(d['name'],'검사 에피소드');self.assertEqual([s['id'] for s in d['steps']],[second,first])
        self.assertEqual(read_json(self.data/'preferences.json')['last_episode_id'],a.episode['id'])
    def test_save_actions_remain_visible_at_1280_by_800(self):
        a=self.app
        self.assertLessEqual(a.capture_btn.winfo_rooty()+a.capture_btn.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
        self.assertGreater(a.preview_canvas.winfo_width(),350)
        self.assertGreater(a.joint_controls.winfo_height(),160)
    def test_roi_letterboxing_uses_source_image_coordinates_and_persists(self):
        a=self.app;a.camera_rect=(100,50,800,450)
        self.assertEqual(a.image_point(500,275),(.5,.5))
        a.roi=[.2,.3,.7,.8];a.save_preferences();a.show_page('teach');a.show_page('camera')
        self.assertEqual(read_json(self.data/'preferences.json')['camera_roi'],[.2,.3,.7,.8])

    def test_sim_adjustment_changes_model_only_and_survives_reload(self):
        import math
        from tests.fixtures import load_profile
        a=self.app;a.commit_target();ticks=a.target.copy();cal=(self.data/'calibration/follower.json').read_bytes()
        old=a.reference.angles(ticks)
        a.trim_vars['shoulder_lift'].set(12);a.table_z.set('2.4');a.save_model()
        self.assertAlmostEqual(a.reference.angles(ticks)[1]-old[1],12*2*math.pi/4096)
        self.assertEqual(a.reference.ticks_from_angles(a.reference.angles(ticks)),ticks)
        self.assertEqual(a.episode['steps'][0]['ticks'],ticks);self.assertEqual((self.data/'calibration/follower.json').read_bytes(),cal)
        a.close();self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False)
        self.assertEqual(self.app.reference.trims['shoulder_lift'],12);self.assertAlmostEqual(self.app.profile['table_z_mm'],-4.8)
    def test_preview_holds_jig_and_stops_release_it(self):
        a=self.app;a.commit_target();a.play();self.assertTrue(a.pose_latch.frozen)
        with self.assertRaises(ValueError):a.refresh_jig_reference()
        a.stop_preview();self.assertFalse(a.pose_latch.frozen)
    def test_motor_program_hold_does_not_depend_on_dropped_status_events(self):
        from so101_teach.motion import MotionSession
        a=self.app;s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);s.running=True;s.state='HOLD';a.session=s
        try:
            a.motion_request('move',[a.target]);self.assertTrue(a.pose_latch.frozen)
            s.publish('motion',{'state':'HOLD','detail':'old status'})
            self.root.after_cancel(a.job);a.poll();self.assertTrue(a.pose_latch.frozen)
            s.program_active.clear();self.root.after_cancel(a.job);a.poll();self.assertFalse(a.pose_latch.frozen)
        finally:a.session=None
    def test_preview_is_rightmost_and_model_adjustment_also_has_preview(self):
        a=self.app
        self.assertGreater(a.preview_canvas.winfo_rootx(),a.joint_controls.winfo_rootx())
        a.show_page('model');self.root.update()
        self.assertTrue(a.model_canvas.winfo_ismapped());self.assertGreater(a.model_canvas.winfo_width(),300)

    def fake_jig_camera(self,pose=(228,-138,67),measured=None):
        from types import SimpleNamespace
        import numpy as np
        now=time.monotonic()
        chosen={'metric':{'center_xy_mm':list(pose[:2]),'yaw_deg':pose[2],'symmetry_deg':90},'center_px':[727,424],'image_angle_deg':20}
        self.app.camera=SimpleNamespace(running=True,error=None,observation=(np.zeros((720,1280,3),np.uint8),{'selected':chosen,'candidates':[],'pose_measured_at':now if measured is None else measured},now),close=lambda:None,join=lambda n:True)
    def test_follow_jig_checkbox_persists_reference_without_modifying_fixed_step(self):
        a=self.app;self.fake_jig_camera();a.apply_target(read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks'])
        a.follow_jig.set(True);a.commit_target()
        step=a.episode['steps'][0];self.assertEqual(step['jig_id'],'pallet');self.assertEqual(step['jig_reference']['pose'],[228,-138,67])
        self.fake_jig_camera((233,-138,70));a.update_selected_step()
        self.assertEqual(a.episode['steps'][0]['jig_reference']['pose'],[228,-138,67])
        a.follow_jig.set(False);a.update_selected_step();self.assertIsNone(a.episode['steps'][0]['jig_id'])
    def test_run_waits_for_new_jig_measurement_returns_and_uses_same_plan_for_preview_and_hardware(self):
        from copy import deepcopy
        a=self.app;self.fake_jig_camera();a.apply_target(read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks'])
        a.follow_jig.set(True);a.commit_target();steps=deepcopy(a.episode['steps']);a.play()
        self.assertEqual(a.page,'camera');self.assertIsNotNone(a.pending_execution)
        a.check_pending_execution();self.assertFalse(a.playing)
        self.fake_jig_camera((233,-138,70));a.check_pending_execution()
        self.assertTrue(a.playing);self.assertEqual(a.page,'teach');self.assertTrue(a.pose_latch.frozen)
        target=deepcopy(a.play_targets);self.assertNotEqual(target[0],steps[0]['ticks'])
        a.stop_preview()
        with patch.object(a,'motion_request') as request:
            a.begin_execution('play',steps,a.current_jig_reference());request.assert_called_once_with('play',target)
        self.assertEqual(a.episode['steps'],steps)
    def test_torque_release_cancels_pending_camera_execution(self):
        from so101_teach.motion import MotionSession
        a=self.app;s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);s.running=True;s.state='HOLD';a.session=s
        try:
            a.pending_execution={'started':time.monotonic()};a.motion_request('release');self.assertIsNone(a.pending_execution);self.assertTrue(s.release_requested.is_set())
        finally:a.session=None
    def test_no_detection_timeout_never_sends_uncorrected_motor_targets(self):
        a=self.app;a.pending_execution={'started':time.monotonic()-a.measurement_timeout_seconds-1,'page':'teach','action':'play','steps':[]}
        with patch.object(a,'motion_request') as request:
            a.check_pending_execution();request.assert_not_called();self.assertIsNone(a.pending_execution)
        self.assertIn('지그 측정 실패',a.message.get())

    def test_six_joint_controls_and_monitor_rows_fit_at_1280_by_800(self):
        a=self.app;self.root.update()
        for widget in (*a.spins.values(),*a.sliders.values()):
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),a.joint_controls.winfo_rooty()+a.joint_controls.winfo_height())
        a.show_page('devices');self.root.geometry('1920x1080');self.root.update()
        for card in a.dual_monitor.cards.values():
            box=card['tree'].bbox('gripper');self.assertTrue(box)
            self.assertLessEqual(box[1]+box[3],card['tree'].winfo_height())

    def render_once(self):
        self.root.after_cancel(self.app.render_job);self.app.render_tick()

    def test_live_view_explains_fault_instead_of_waiting_for_a_closed_session(self):
        from types import SimpleNamespace
        a=self.app;a.set_mode('live')
        a.session=SimpleNamespace(running=False,error='wrist_roll: 목표에서 128틱 이상 크게 이탈했습니다.')
        try:
            self.render_once()
            message=a.preview_canvas.itemcget(a.preview_canvas.find_withtag('stale')[0],'text')
            self.assertIn('실물 연결 오류',message);self.assertIn('wrist_roll',message)
            self.assertIsNone(a.last_fk_angles)
        finally:a.session=None

    def test_mouse_drag_reverses_both_orbit_axes(self):
        from types import SimpleNamespace
        a=self.app;a.drag_start=(100,100,(100.,-18.,.82))
        a.rotate_view(SimpleNamespace(x=120,y=110))
        self.assertEqual(a.view,(90.,-21.,.82))
        a.rotate_view(SimpleNamespace(x=120,y=10000));self.assertEqual(a.view[1],-80)

    def test_completed_frame_is_shown_even_with_a_newer_request_pending(self):
        from types import SimpleNamespace
        a=self.app;fake=SimpleNamespace(token=9,poll=lambda:('frame',8,b'image',a.render_context,time.monotonic()),close=lambda:None)
        a.renderer=fake
        with patch('so101_teach.ui.Image.open') as image,patch.object(a,'fit_image') as show:
            self.render_once();show.assert_called_once();self.assertEqual(a.displayed_token,8)

    def test_mode_switch_discards_old_source_and_clears_previous_image(self):
        from types import SimpleNamespace
        a=self.app;previous=a.render_context;a.last_rgb=object();a.set_mode('live');a.set_mode('target')
        self.assertIsNone(a.last_rgb)
        a.renderer=SimpleNamespace(token=2,poll=lambda:('frame',1,b'image',previous,time.monotonic()),close=lambda:None)
        with patch.object(a,'fit_image') as show:
            self.render_once();show.assert_not_called()

    def test_slow_startup_frame_is_retried_but_not_displayed_as_live(self):
        from types import SimpleNamespace
        a=self.app;a.set_mode('live');a.live_was_fresh=True
        a.session=SimpleNamespace(running=True)
        a.latest=Snapshot('follower',a.reference.middle,{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        a.last_render='submitted'
        a.renderer=SimpleNamespace(token=1,poll=lambda:('frame',1,b'image',a.render_context,time.monotonic()-1),close=lambda:None)
        try:
            with patch.object(a,'fit_image') as show:
                self.render_once();show.assert_not_called();self.assertIsNone(a.last_render)
        finally:a.session=None

    def test_same_mode_slider_keeps_displayed_model_until_replacement_frame(self):
        from PIL import Image
        a=self.app;a.show_page('model');self.root.update()
        a.fit_image(a.model_canvas,Image.new('RGB',(200,150),'red'))
        image_id=a.model_canvas.find_withtag('image');context=a.render_context
        for tick in (900,901,902):
            a.slider_tick('gripper',tick)
            self.assertEqual(a.model_canvas.find_withtag('image'),image_id)
            self.assertEqual(a.render_context,context)
        a.set_mode('live');self.assertFalse(a.model_canvas.find_withtag('image'))
        self.assertGreater(a.render_context,context)

    def test_camera_and_preview_update_image_in_place_preserving_overlay(self):
        from PIL import Image
        a=self.app
        for canvas in (a.camera_canvas,a.preview_canvas,a.model_canvas):
            a.fit_image(canvas,Image.new('RGB',(200,150),'red'))
            image_id=canvas.find_withtag('image');old_photo=canvas.photo
            overlay=canvas.create_rectangle(0,0,10,10,tags='test-overlay')
            rect=a.fit_image(canvas,Image.new('RGB',(100,200),'blue'))
            self.assertEqual(canvas.find_withtag('image'),image_id)
            self.assertIsNot(canvas.photo,old_photo)
            self.assertEqual(canvas.coords(image_id[0]),list(map(float,rect[:2])))
            self.assertGreater(canvas.find_all().index(overlay),canvas.find_all().index(image_id[0]))

    def test_waiting_message_is_not_recreated_on_every_frame(self):
        a=self.app;a.set_mode('live');self.render_once();first=a.preview_canvas.find_withtag('stale')
        self.assertTrue(first)
        for _ in range(3):
            self.render_once();self.assertEqual(a.preview_canvas.find_withtag('stale'),first)

    def test_ready_window_restores_opacity_after_layout_is_mapped(self):
        from so101_teach.ui import show_ready_window
        self.root.withdraw();show_ready_window(self.root);self.root.update()
        self.assertTrue(self.root.winfo_ismapped())
        deadline=time.monotonic()+.3
        while time.monotonic()<deadline:
            self.root.update();time.sleep(.01)
        self.assertEqual(float(self.root.attributes('-alpha')),1.)
        self.assertGreater(self.app.preview_canvas.winfo_width(),350)

    def test_close_unmaps_parent_before_destroying_its_children(self):
        root=self.root;events=[]
        original_withdraw=root.withdraw;original_destroy=root.destroy
        def withdraw():events.append('withdraw');return original_withdraw()
        def destroy():
            events.append('destroy');self.assertFalse(root.winfo_ismapped());return original_destroy()
        with patch.object(root,'withdraw',withdraw),patch.object(root,'destroy',destroy):self.app.close()
        self.assertEqual(events,['withdraw','destroy'])



    def test_jig_follow_states_fit_joint_container_and_actions_at_minimum_window(self):
        from so101_teach.domain import LABELS
        a=self.app
        for n,label in zip(JOINTS,LABELS):a.current_vars[n].set(f'{label} · 현재 4095')
        for width,height in ((1180,760),(1280,800),(1480,920)):
            self.root.geometry(f'{width}x{height}')
            for state in ('off','new','saved'):
                with self.subTest(size=(width,height),state=state):
                    a.episode['steps']=[];a.episode['jig_references']={};a.selected=None
                    if state=='saved':
                        reference={'pose':[-1999.9,1999.9,-179.9],'symmetry_deg':360,'stl_sha256':a.catalog.mesh(a.active_jig)['sha256']}
                        step=a.store.step(a.target,'지그 기준 자세');step.update(jig_id=a.active_jig,jig_reference=reference)
                        a.episode['steps']=[step];a.selected=step['id'];a.episode['jig_references']={a.active_jig:reference}
                    a.follow_jig.set(False)
                    if state!='off':a.follow_jig_check.invoke()
                    else:a.update_jig_hint()
                    self.root.update()
                    group=a.joint_controls;rows=group.winfo_children()
                    gaps=[b.winfo_y()-r.winfo_y()-r.winfo_height() for r,b in zip(rows,rows[1:])]
                    self.assertGreaterEqual(min(gaps),12 if height>=800 else 4)
                    self.assertLessEqual(max(gaps),16)
                    for row in rows:
                        self.assertLessEqual(row.winfo_y()+row.winfo_height(),group.winfo_height())
                        for child in row.winfo_children():
                            self.assertLessEqual(child.winfo_x()+child.winfo_width(),row.winfo_width())
                            self.assertLessEqual(child.winfo_y()+child.winfo_height(),row.winfo_height())
                    self.assertLessEqual(group.winfo_rooty()+group.winfo_height(),a.shortcut_hint.winfo_rooty())
                    for button in (a.capture_btn,a.add_step_btn,a.update_step_btn,a.move_btn):
                        parent=button.master
                        self.assertLessEqual(button.winfo_y()+button.winfo_height(),parent.winfo_height())
                        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),self.root.winfo_rooty()+height)

if __name__=='__main__':unittest.main()
