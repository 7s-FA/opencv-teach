import unittest,time
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace
from . import test_ui as fixtures
from so101_teach.domain import ROOT,read_json,JOINTS
from so101_teach.motion import MotionSession

class FeatureUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def teach(self):
        a=self.app;self.fake_jig_camera();a.apply_target(read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks']);a.follow_jig.set(True);a.commit_target();return a
    def test_safe_boundaries_insert_once_and_new_steps_go_inside(self):
        a=self.teach();a.add_safe_steps();self.assertEqual([s.get('safe_boundary') for s in a.episode['steps']],['start',None,'end'])
        a.add_safe_steps();self.assertEqual(len(a.episode['steps']),3)
        a.selected=None;a.follow_jig.set(False);a.commit_target();self.assertEqual(len(a.episode['steps']),4);self.assertEqual(a.episode['steps'][-1]['safe_boundary'],'end')
        a.store.validate(a.episode)
    def test_safe_arrival_then_detection_not_before_arrival(self):
        a=self.teach();a.add_safe_steps();s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);s.running=True;s.state='HOLD';a.session=s
        try:
            a.execute_episode();self.assertIsNotNone(a.safe_entry);self.assertIsNone(a.pending_execution)
            self.assertEqual(s.commands.get_nowait()[1],[a.episode['steps'][0]['ticks']])
            s.program_active.clear();s.completed_request_id=a.safe_entry['request_id'];s.motion_log.append({'state':'HOLD','detail':'실물 스텝 실행 완료'});a.poll_extended()
            self.assertIsNone(a.safe_entry);self.assertIsNotNone(a.pending_execution);self.assertEqual(a.page,'camera')
        finally:a.session=None
    def test_safe_pause_cancels_following_execution(self):
        a=self.teach();a.add_safe_steps();s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);s.running=True;s.state='HOLD';a.session=s
        try:
            a.execute_episode();s.program_active.clear();s.motion_log.append({'state':'HOLD','detail':'이동 정지'});a.poll_extended()
            self.assertIsNone(a.pending_execution);self.assertIsNone(a.safe_entry)
        finally:a.session=None
    def test_multiple_jigs_keep_separate_references_and_roi(self):
        a=self.teach();first=deepcopy(a.episode['steps'][0]);second=a.catalog.duplicate('pallet');key=second['id'];a.catalog_changed();a.set_step_jig(key)
        self.fake_jig_camera((231,-137,68));frame,out,at=a.camera.observation
        out['by_jig']={'pallet':deepcopy(out),key:deepcopy(out)};out['by_jig']['pallet']['selected']['metric']['center_xy_mm']=[228,-138]
        a.selected=None;a.step_name.set('두 번째 지그');a.commit_target()
        self.assertEqual(a.episode['steps'][0],first);self.assertEqual(a.episode['steps'][1]['jig_id'],key)
        self.assertEqual(a.episode['steps'][1]['jig_reference']['pose'],[231,-137,68])
        a.select_camera_jig(key);a.roi=[.5,.2,.9,.8];a.save_preferences();self.assertEqual(a.catalog.items[key]['roi'],a.roi);self.assertIsNone(a.catalog.items['pallet']['roi'])
    def test_multiple_jigs_require_every_fresh_measurement(self):
        a=self.teach();second=a.catalog.duplicate('pallet');key=second['id'];a.catalog_changed();a.set_step_jig(key)
        self.fake_jig_camera();frame,out,at=a.camera.observation;out['by_jig']={'pallet':deepcopy(out),key:deepcopy(out)}
        a.selected=None;a.commit_target();a.play();self.assertIsNotNone(a.pending_execution)
        self.fake_jig_camera();a.check_pending_execution();self.assertFalse(a.playing)
        frame,out,at=a.camera.observation;out['by_jig']={'pallet':deepcopy(out),key:deepcopy(out)};a.check_pending_execution();self.assertTrue(a.playing)
    def test_tcp_manual_and_model_restore_do_not_touch_motor_ticks(self):
        a=self.app;s=a.settings;ticks=a.target.copy();cal=(self.data/'calibration/follower.json').read_bytes();s.vars['tcp_mode'].set('직접 설정');s.vars['tcp_xyz_X'].set('5');s.save_tcp()
        self.assertEqual(a.profile['tcp']['xyz_mm'][0],5);self.assertEqual(a.target,ticks)
        s.load_model_tcp();s.save_tcp();self.assertAlmostEqual(a.profile['tcp']['xyz_mm'][0],1.773409675938293)
        self.assertEqual((self.data/'calibration/follower.json').read_bytes(),cal)
    def test_save_as_and_space_help_are_in_window(self):
        a=self.app;a.commit_target();before=a.episode['id'];a.save_as_episode();self.assertNotEqual(a.episode['id'],before);self.assertEqual(len(a.store.entries()),2)
        a.open_help();self.assertEqual(a.page,'settings');self.assertEqual(a.settings.tabs.select(),str(a.settings.pages['help']))
    def test_profile_save_reload_keeps_camera_and_model_values(self):
        a=self.app;s=a.settings;camera=deepcopy(a.profile['extrinsics']);s.vars['robot_name'].set('시험 팔');s.vars['mode'].set('데모');s.save_profile()
        self.assertEqual(a.profile['mode'],'demo');self.assertEqual(a.profile['extrinsics'],camera);self.assertEqual(a.profile['tcp']['mode'],'model')
        self.assertEqual(len(s.library.items),1)
    def test_jig_heading_controls_removed(self):
        self.assertNotIn('jig_yaw',self.app.settings.vars)
        self.assertFalse(hasattr(self.app.settings,'capture_heading'))

    def test_demo_end_to_end_safe_entry_measure_work_and_safe_return(self):
        from so101_teach.demo import DemoSession
        import cv2
        a=self.app;a.profile['mode']='demo';frame=cv2.imread(str(ROOT/'verification/pallet-current.jpg'))
        def camera_update():
            out=a.detector.process(frame);a.camera=SimpleNamespace(error=None,running=True,observation=(frame,out,time.monotonic()),close=lambda:None,join=lambda n:True)
        def pump_until(condition,seconds=5):
            end=time.monotonic()+seconds
            while not condition() and time.monotonic()<end:camera_update();self.root.update();time.sleep(.02)
            self.assertTrue(condition(),a.message.get())
        pump_until(lambda:bool(a.current_jig_reference()));a.follow_jig.set(True);target=a.reference.middle.copy();target['shoulder_pan']+=4;a.apply_target(target);a.commit_target();a.apply_target(a.reference.middle);a.add_safe_steps()
        with patch('so101_teach.devices.new_bus',side_effect=AssertionError('No physical USB')):
            a.toggle_connection()
            try:
                pump_until(lambda:a.latest is not None);a.motion_request('arm');pump_until(lambda:a.session.state=='HOLD')
                a.execute_episode();pump_until(lambda:a.safe_entry is None and a.pending_execution is None and not a.session.program_active.is_set() and a.session.state=='HOLD')
                self.assertEqual(a.session.last_goals,a.reference.middle);self.assertEqual(a.last_plan[0]['corrected'],True)
            finally:a.session.close();a.session.join(1);a.camera=None

    def test_old_completion_does_not_advance_a_rejected_safe_entry(self):
        a=self.teach();a.add_safe_steps();s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);s.running=True;s.state='HOLD';s.completed_request_id=0;a.session=s
        try:
            a.execute_episode();s.program_active.clear();s.motion_log.append({'state':'HOLD','detail':'실물 스텝 실행 완료'});a.poll_extended()
            self.assertIsNone(a.pending_execution);self.assertIsNone(a.safe_entry)
        finally:a.session=None
    def test_invalid_profile_does_not_overwrite_active_file(self):
        a=self.app;before=(self.data/'profile.json').read_bytes();bad=deepcopy(a.profile);bad['model_reference']['calibration_sha256']='wrong'
        with self.assertRaises(ValueError):a.install_profile(bad)
        self.assertEqual((self.data/'profile.json').read_bytes(),before)

    def test_model_inspector_drafts_never_save_tcp_or_jig_settings(self):
        a=self.app;s=a.settings;before=(self.data/'profile.json').read_bytes();items=deepcopy(a.catalog.items)
        s.vars['tcp_mode'].set('직접 설정');s.vars['tcp_xyz_X'].set('8')
        spec=s.model_spec('tcp');self.assertEqual(spec['tcp']['xyz_mm'][0],8)
        s.vars['jig_width'].set('999');s.vars['jig_stl'].set('')
        self.assertEqual(s.model_spec('jigs')['size_mm'][0],999)
        self.assertEqual((self.data/'profile.json').read_bytes(),before);self.assertEqual(a.catalog.items,items)
        self.assertIsNone(a.session)
    def test_model_inspector_stays_beside_scrollable_fields_at_1280(self):
        a=self.app;s=a.settings;a.show_page('settings');s.tabs.select(s.pages['tcp']);self.root.update()
        pane=s.model_views['tcp'];fields=s.scroll_canvases[str(s.pages['tcp'])]
        self.assertGreater(pane.canvas.winfo_width(),300)
        self.assertGreater(pane.winfo_rootx(),fields.winfo_rootx()+fields.winfo_width())
        self.assertLessEqual(pane.winfo_rootx()+pane.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())

    def test_display_transition_never_replaces_execution_measurement(self):
        a=self.app;self.fake_jig_camera((228,-138,67));raw=a.camera.observation[1]['selected']
        a.display_poses.update('pallet',raw,time.monotonic())
        self.fake_jig_camera((248,-138,72));raw=a.camera.observation[1]['selected']
        shown=a.display_poses.update('pallet',raw,time.monotonic())
        self.assertAlmostEqual(shown['metric']['center_xy_mm'][0],228)
        ref=a.current_jig_reference('pallet')
        self.assertEqual(ref['pose'],[248,-138,72]);self.assertEqual(raw['metric']['center_xy_mm'],[248,-138])

    def test_new_step_never_overwrites_selection_and_update_is_explicit(self):
        a=self.app;a.commit_target();self.root.update();first=deepcopy(a.episode['steps'][0])
        a.slider_tick('gripper',900);a.step_name.set('새 자세');a.add_step_btn.invoke();self.root.update()
        self.assertEqual(len(a.episode['steps']),2);self.assertEqual(a.episode['steps'][0],first)
        second=a.selected;a.slider_tick('gripper',910);a.update_selected_step();self.root.update()
        self.assertEqual(len(a.episode['steps']),2);self.assertEqual(a.episode['steps'][1]['id'],second)
        saved=a.store.load(a.save_episode());self.assertEqual(saved['steps'][0],first);self.assertEqual(saved['steps'][1]['ticks']['gripper'],910)
        a.selected=None
        with self.assertRaises(ValueError):a.update_selected_step()
        self.assertEqual(len(a.episode['steps']),2)

    def test_safe_pose_edit_apply_reopen_and_cancel_keep_work_steps(self):
        a=self.app;a.commit_target();self.root.update();work=deepcopy(a.episode['steps'][0]);draft=a.target.copy()
        a.safe_edit_btn.invoke();self.root.update();self.assertTrue(a.safe_editing)
        self.assertTrue(a.preview_canvas.winfo_ismapped());self.assertTrue(a.follow_jig_check.instate(['disabled']))
        self.assertEqual(a.add_step_btn.cget('text'),'안전 자세 적용')
        a.slider_tick('gripper',900);a.add_step_btn.invoke();self.root.update();self.assertFalse(a.safe_editing)
        self.assertEqual(a.episode['steps'][1],work)
        for step in (a.episode['steps'][0],a.episode['steps'][-1]):
            self.assertEqual(step['ticks']['gripper'],900);self.assertFalse(step.get('jig_id'))
        with self.assertRaises(ValueError):a.update_selected_step()
        a.apply_target(draft);a.step_name.set('저장 전 초안');a.begin_safe_edit();self.root.update()
        self.assertEqual(a.target['gripper'],900)
        a.slider_tick('gripper',910);a.safe_edit_btn.invoke();self.root.update()
        self.assertEqual(a.target,draft);self.assertEqual(a.step_name.get(),'저장 전 초안')
        self.assertEqual(a.episode['steps'][0]['ticks']['gripper'],900)
        a.begin_safe_edit();a.steps.selection_set(work['id']);self.root.update()
        self.assertFalse(a.safe_editing);self.assertEqual(a.selected,work['id'])

    def test_speed_preference_stages_next_move_without_commands(self):
        a=self.app;s=MotionSession('fake',a.calibration,bus_factory=lambda *args:None);a.session=s
        try:
            self.assertEqual(a.motion_speed_choice.get(),'보통');self.assertEqual(s.rate_ticks_s,350)
            self.assertEqual(a.preferences['motion_speed_version'],2)
            a.motion_speed_choice.set('빠르게');a.change_motion_speed()
            self.assertEqual(s.rate_ticks_s,400);self.assertTrue(s.commands.empty());self.assertFalse(s.command_log)
            self.assertEqual(read_json(self.data/'preferences.json')['motion_speed'],'빠르게')
            s.state='MOVING';a.motion_speed_choice.set('느리게')
            with self.assertRaises(ValueError):a.change_motion_speed()
            self.assertEqual(s.rate_ticks_s,400);self.assertEqual(a.motion_speed_choice.get(),'빠르게')
        finally:a.session=None

    def test_settings_model_paint_reuses_image_and_clears_invalid_model(self):
        from PIL import Image
        pane=self.app.settings.model_views['tcp'];pane.image=Image.new('RGB',(200,150),'red');pane.meta={'markers':{}}
        pane.paint();first=pane.canvas.find_withtag('model-image');self.assertTrue(first)
        pane.image=Image.new('RGB',(150,200),'blue');pane.paint();self.assertEqual(pane.canvas.find_withtag('model-image'),first)
        pane.image=None;pane.paint();self.assertFalse(pane.canvas.find_withtag('model-image'))

    def test_calibration_reference_buttons_are_visual_only_and_keep_preview_beside_form(self):
        a=self.app;s=a.settings;pane=s.calibration_panel;a.show_page('settings');s.tabs.select(s.pages['calibration']);self.root.update()
        fields=s.scroll_canvases[str(s.pages['calibration'])]
        self.assertGreater(pane.canvas.winfo_width(),300);self.assertGreater(pane.canvas.winfo_rootx(),fields.winfo_rootx()+fields.winfo_width())
        with patch('so101_teach.devices.new_bus',side_effect=AssertionError('no device access')):
            pane.show('positive');spec,caption=pane.spec();self.assertAlmostEqual(spec[0][0],a.reference.radians['shoulder_pan']+1.5707963267948966)
            self.assertEqual(spec[2]['highlight_joint'],'shoulder_pan');self.assertIn('실물 구동 없음',caption)
            self.assertIsNone(a.session);self.assertIsNone(s.worker)
        self.assertTrue(pane.zero.instate(['disabled']));pane.state_changed('ANGLES');self.assertTrue(pane.zero.instate(['!disabled']));self.assertTrue(pane.negative.instate(['disabled']));self.assertTrue(pane.save.instate(['disabled']))

    def test_three_point_profile_disables_old_trim_and_preserves_existing_step_ticks(self):
        from tests.test_angle_calibration import points
        from so101_teach.angle_mapping import angle_path
        from so101_teach.domain import atomic_json
        from tests.fixtures import load_profile
        a=self.app;a.commit_target();original=deepcopy(a.episode['steps']);a.trim_vars['shoulder_lift'].set(12);a.save_model()
        source=self.data/'copy.json';source.write_bytes((self.data/'calibration/follower.json').read_bytes());atomic_json(angle_path(source),points(a.calibration))
        path,cal=a.settings.library.copy_calibration(source);profile=deepcopy(a.profile);profile['calibration_file']=path;a.install_profile(profile)
        self.assertEqual(a.episode['steps'],original);self.assertTrue(all(v==0 for v in a.reference.trims.values()))
        self.assertTrue(all(w.instate(['disabled']) for w in a.trim_controls));self.assertIsNotNone(load_profile(self.data)[1].angle_mapping)
        a.close();self.root=__import__('tkinter').Tk();self.app=__import__('so101_teach.ui',fromlist=['App']).App(self.root,self.data,render=False,auto_camera=False)
        self.assertTrue(all(v==0 for v in self.app.reference.trims.values()));self.assertIsNotNone(self.app.calibration.angle_mapping)

    def test_calibration_selection_event_does_not_overwrite_custom_angle_input(self):
        s=self.app.settings;p=s.calibration_panel
        s.vars['cal_joint'].set('어깨 들기');p.select_combo();s.vars['cal_negative'].set('-60');p.show('negative');self.root.update()
        self.assertEqual(s.value('cal_negative'),'-60');spec,caption=p.spec();self.assertIn('-60',caption)
        p.select();self.assertEqual(s.value('cal_negative'),'-60')
