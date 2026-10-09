import json,shutil,tempfile,time,unittest
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from tests.fixtures import DATA
from tests import test_ui
from so101_teach.domain import ROOT,EpisodeStore,load_profile,read_json,atomic_json
from so101_teach.pi_episode_library import import_snapshot,digest
from so101_teach.episode_events import events_for,set_event

class EpisodeSyncTests(unittest.TestCase):
    def test_automatic_refresh_keeps_local_review_when_pi_is_unchanged(self):
        import_snapshot(self.store,self.data,self.snapshot)
        review=deepcopy(self.doc);review['product_type']='A';review['startup_inspections']=[{'station':'carrier','target':'all','expected':'present','timeout_seconds':10}];self.store.save(review)
        for _ in range(2):
            record,preserved=import_snapshot(self.store,self.data,self.snapshot,preserve_local_changes=True)
            self.assertEqual(self.store.load(self.store.directory/(review['id']+'.json')),review);self.assertFalse(preserved)
            self.assertEqual(record['hashes'][review['id']],digest(self.doc))
        self.assertEqual(len(self.store.entries()),1)
    def test_changed_pi_still_preserves_local_review_before_replacing(self):
        import_snapshot(self.store,self.data,self.snapshot);review=deepcopy(self.doc);review['product_type']='A';self.store.save(review)
        self.doc['name']='Pi에서 새로 변경'
        _,preserved=import_snapshot(self.store,self.data,self.snapshot,preserve_local_changes=True)
        self.assertEqual(len(preserved),1);self.assertEqual(self.store.load(self.store.directory/(preserved[0]+'.json'))['product_type'],'A')
        self.assertEqual(self.store.load(self.store.directory/(self.doc['id']+'.json')),self.doc)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        shutil.copy2(DATA/'profile.json',self.data/'profile.json');shutil.copytree(DATA/'calibration',self.data/'calibration')
        _,cal,ref=load_profile(self.data);self.store=EpisodeStore(self.data/'episodes',cal,'arm2')
        self.doc=self.store.new('Pi A');self.doc['steps']=[self.store.step(ref.middle,'하단 : 놓기'),self.store.step(ref.middle,'하단 : 조정 완료')]
        self.snapshot={'arm':'arm2','calibration_sha256':cal.sha256,'episodes':[self.doc],'recipes':{'A':{'episode_id':self.doc['id'],'timeout_seconds':900}},'settings':{'speed':400,'hold_seconds':10,'acquisition_seconds':5,'acquisition_attempts':3}}
    def tearDown(self):self.temp.cleanup()
    def test_import_preserves_local_conflict_and_never_changes_source(self):
        local=deepcopy(self.doc);local['name']='편집한 로컬';self.store.save(local);before=deepcopy(self.snapshot)
        record,preserved=import_snapshot(self.store,self.data,self.snapshot)
        self.assertEqual(self.snapshot,before);self.assertEqual(len(preserved),1)
        self.assertEqual(self.store.load(self.store.directory/(self.doc['id']+'.json')),self.doc)
        self.assertEqual(self.store.load(self.store.directory/(preserved[0]+'.json'))['steps'],local['steps'])
        self.assertEqual(record['hashes'][self.doc['id']],digest(self.doc))
    def test_remote_update_replaces_clean_cache_without_duplicate_drafts(self):
        import_snapshot(self.store,self.data,self.snapshot)
        self.doc['name']='Pi에서 변경';_,preserved=import_snapshot(self.store,self.data,self.snapshot)
        self.assertFalse(preserved);self.assertEqual(len(self.store.entries()),1)
    def test_wrong_arm_or_calibration_rejected_before_local_writes(self):
        for change in ({'arm':'arm3'},{'calibration_sha256':'0'*64}):
            with self.assertRaises(ValueError):import_snapshot(self.store,self.data,{**self.snapshot,**change})
        self.assertFalse(self.store.directory.exists())
    def test_failed_import_rolls_back_replaced_episodes(self):
        self.store.save(self.doc);other=deepcopy(self.doc);other['id']='a'*32;self.snapshot['episodes'].append(other)
        changed=deepcopy(self.doc);changed['name']='Pi changed';self.snapshot['episodes'][0]=changed
        original=self.store.save
        def save(doc):
            if doc['id']==other['id']:raise OSError('disk full')
            return original(doc)
        with patch.object(self.store,'save',side_effect=save):
            with self.assertRaises(OSError):import_snapshot(self.store,self.data,self.snapshot)
        self.assertEqual(self.store.load(self.store.directory/(self.doc['id']+'.json')),self.doc)
    def test_explicit_markers_survive_rename_and_reorder(self):
        self.assertEqual(events_for(self.doc)['LOWER'],self.doc['steps'][-1]['id'])
        key=self.doc['steps'][0]['id'];set_event(self.doc,key,'하단 완료')
        self.doc['steps'][0]['name']='이름 변경';self.doc['steps'].reverse()
        self.store.validate(self.doc);self.assertEqual(events_for(self.doc),{'LOWER':key})

class EpisodeAdjustUITests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown
    def test_execution_form_hides_hold_but_preserves_pi_freshness_setting(self):
        a=self.app;a.commit_target();a.episode_sync.snapshot={'settings':{'hold_seconds':5},'fetched_at':1}
        panel=a.episode_adjust_panel;panel.refresh()
        self.assertEqual(set(panel.values),{'speed','acquisition_seconds','acquisition_attempts','timeout_seconds'})
        panel.values['speed'].set('350');panel.save_settings();self.assertEqual(a.episode['pi_execution_settings']['hold_seconds'],5)
        a.episode['pi_execution_settings']['hold_seconds']=7;panel.values['speed'].set('400');panel.save_settings()
        self.assertEqual(a.episode['pi_execution_settings']['hold_seconds'],7)
    def save_screen(self,name):
        from PIL import ImageGrab
        self.root.update_idletasks();self.root.update()
        x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
        out=ROOT/'verification/episode-adjust-sync';out.mkdir(exist_ok=True)
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(out/name)
    def test_dedicated_tab_edit_markers_and_minimum_layout(self):
        a=self.app;self.root.geometry('1920x1080');self.root.update()
        self.assertFalse(a.episode_adjusting());self.assertTrue(a.editor_card.winfo_ismapped())
        a.step_name.set('하단 최종');a.commit_target();key=a.selected
        a.open_episode_adjust();self.root.update();self.assertFalse(a.editor_card.winfo_ismapped())
        before=deepcopy(a.episode['steps']);a.completion_event.set('하단 완료');a.episode_adjust_panel.save_marker()
        self.assertEqual(a.episode['steps'],before);self.assertEqual(events_for(a.episode),{'LOWER':key})
        panel=a.episode_adjust_panel;panel.values['speed'].set('350');panel.values['acquisition_seconds'].set('4');panel.save_settings()
        self.assertEqual(a.episode['pi_execution_settings']['speed'],350)
        self.assertEqual(a.steps.item(key,'values')[2],'하단 완료');self.save_screen('settings-1920x1080.png')
        a.teach_tabs.select(a.teach_overview);self.root.update();self.assertTrue(a.editor_card.winfo_ismapped())
        a.step_name.set('이름 변경');a.update_selected_step();self.assertEqual(events_for(a.episode),{'LOWER':key})
        self.assertLessEqual(a.spins['gripper'].winfo_rooty()+a.spins['gripper'].winfo_height(),a.joint_controls.winfo_rooty()+a.joint_controls.winfo_height())
        self.save_screen('step-edit-1920x1080.png')
    def test_adjustment_preview_follows_selection_without_motor_commands(self):
        a=self.app;self.root.geometry('1920x1080');a.commit_target();first=a.selected
        a.target['shoulder_pan']+=12;a.step_name.set('두 번째 자세');a.commit_target();before=deepcopy(a.episode)
        a.open_episode_adjust();self.root.update()
        with patch.object(a,'motion_request',side_effect=AssertionError('preview must not move motors')):
            a.steps.selection_set(first);a.select_step();self.root.update()
        self.assertEqual(a.target,a.episode['steps'][0]['ticks']);self.assertEqual(a.episode,before)
        for page in (a.episode_adjust_panel.inspection,a.episode_adjust_panel.startup):
            a.episode_adjust_panel.tabs.select(page);self.root.update();self.assertTrue(a.preview_canvas.winfo_ismapped());self.assertGreater(a.preview_canvas.winfo_width(),600)
            self.assertLess(a.episode_policy_card.winfo_rootx(),a.preview_canvas.winfo_rootx())
        self.assertTrue(a.teach_preview_btn.winfo_ismapped());self.assertFalse(a.execute_btn.winfo_ismapped())
        a.teach_tabs.select(a.teach_overview);self.root.update();self.assertTrue(a.editor_card.winfo_ismapped());self.assertTrue(a.preview_canvas.winfo_ismapped())
    def snapshot(self):
        a=self.app;doc=a.store.new('Pi에서 가져온 조립');doc['steps']=[a.store.step(a.reference.middle,'하단 : 놓고 들기')]
        return {'arm':a.profile.get('robot_id','arm2'),'calibration_sha256':a.calibration.sha256,'episodes':[doc],'recipes':{'A':{'episode_id':doc['id'],'timeout_seconds':900}},'settings':{'speed':400,'hold_seconds':10,'acquisition_seconds':5,'acquisition_attempts':3},'execution_policy':{'linear_target_mm':100,'finish_hold_seconds':5}}
    def test_async_import_reads_pi_and_applies_settings_without_motor_commands(self):
        a=self.app;sync=a.episode_sync;future=Future();snapshot=self.snapshot()
        with patch('so101_teach.pi_episode_library.pi_connection.load',return_value={}),patch('so101_teach.pi_episode_library.pi_connection.validate',return_value={}),patch.object(a.settings.pool,'submit',return_value=future),patch.object(a,'motion_request',side_effect=AssertionError('No hardware')):
            sync.refresh();future.set_result(snapshot);sync.poll()
        self.assertTrue(sync.ready);self.assertEqual(a.episode,snapshot['episodes'][0]);self.assertEqual(a.acquisition_seconds.get(),'5')
        self.assertIn('하단 완료',a.episode_policy.get());self.assertIn('[Pi A]',a.library.get(0))
    def test_late_import_does_not_replace_new_editor_draft(self):
        a=self.app;sync=a.episode_sync;future=Future()
        sync.start_signature=sync.editor_signature();sync.job=future;a.step_name.set('받는 동안 입력한 내용');future.set_result(self.snapshot());sync.poll()
        self.assertFalse(sync.ready);self.assertEqual(a.step_name.get(),'받는 동안 입력한 내용')
        self.assertIn('편집',a.pi_episode_status.get())
    def test_manual_pull_preserves_unsaved_name_and_pose(self):
        a=self.app;a.commit_target();a.episode_name.set('아직 저장하지 않은 이름')
        with self.assertRaisesRegex(ValueError,'이름을 저장'):a.episode_sync.refresh()
        a.episode_name.set(a.episode['name']);a.step_name.set('아직 저장하지 않은 자세')
        with self.assertRaisesRegex(ValueError,'편집한 자세'):a.episode_sync.refresh()
        self.assertIsNone(a.episode_sync.job)

class ActualEpisodeLayoutTests(unittest.TestCase):
    def test_startup_fetches_both_pi_libraries_without_uploading_or_devices(self):
        import tkinter as tk
        from so101_teach.ui import App
        from so101_teach.arm_workspaces import ArmWorkspaces
        from so101_teach.pi_connection import DEFAULTS
        snapshots={key:read_json(ROOT/f'verification/episode-adjust-sync/{key}-snapshot.json') for key in ('arm2','arm3')}
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            for name in ('profile.json','robot_profiles.json','jigs.json'):shutil.copy2(ROOT/'data'/name,folder/name)
            shutil.copytree(ROOT/'data/calibration',folder/'calibration')
            root=tk.Tk();root.withdraw();manager=None
            try:
                with patch.object(App,'startup_connections'),patch.object(App,'start_linear_state_query'),patch('so101_teach.pi_episode_library.pi_connection.load',return_value=dict(DEFAULTS)),patch('so101_teach.pi_episode_library.pi_connection.validate',return_value=dict(DEFAULTS)),patch('so101_teach.pi_episode_library.episode_transfer.remote_request',side_effect=lambda config,request:deepcopy(snapshots[request['arm']])) as requests:
                    manager=ArmWorkspaces(root,folder,render=False,auto_camera=False,auto_devices=True)
                    until=time.monotonic()+3
                    while not all(a.episode_sync.ready for a in manager.apps.values()) and time.monotonic()<until:root.update();time.sleep(.02)
                    self.assertTrue(all(a.episode_sync.ready for a in manager.apps.values()))
                    self.assertEqual({c.args[1]['arm'] for c in requests.call_args_list},{'arm2','arm3'})
                    self.assertTrue(all(c.args[1]['operation']=='pull' for c in requests.call_args_list))
                    for key,a in manager.apps.items():
                        self.assertEqual(a.episode['id'],snapshots[key]['recipes']['A']['episode_id']);self.assertIsNone(a.session)
            finally:
                if manager:
                    manager.close()
                    for _ in range(5):
                        try:root.update()
                        except tk.TclError:break
                else:root.destroy()
    def test_actual_pi_episode_and_camera_at_two_window_sizes(self):
        import tkinter as tk
        import cv2
        from types import SimpleNamespace
        from PIL import ImageGrab
        from so101_teach.ui import App
        snapshot_path=ROOT/'verification/episode-adjust-sync/arm2-snapshot.json'
        if not snapshot_path.exists():self.skipTest('Actual read-only Pi snapshot not present')
        cfg=read_json(ROOT/'tests/fixtures/insert-normal/fixture.json');profile=cfg['profile']
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);atomic_json(folder/'profile.json',profile);atomic_json(folder/'jigs.json',cfg['catalog'])
            shutil.copytree(ROOT/'data/calibration',folder/'calibration')
            root=tk.Tk();root.withdraw();a=App(root,folder,render=True,auto_camera=False);root.deiconify()
            try:
                snapshot=read_json(snapshot_path);sync=a.episode_sync;sync.start_signature=sync.editor_signature();sync.job=Future();sync.job.set_result(snapshot);sync.poll();self.assertTrue(sync.ready)
                for size in ('1480x920','1180x760'):
                    root.geometry(size);a.teach_tabs.select(a.teach_overview);root.update()
                    until=time.monotonic()+15
                    while a.last_rgb is None and time.monotonic()<until:root.update();time.sleep(.04)
                    self.assertIsNotNone(a.last_rgb);root.update()
                    self.assertGreater(a.steps.winfo_height(),75)
                    self.assertLessEqual(a.spins['gripper'].winfo_rooty()+a.spins['gripper'].winfo_height(),a.joint_controls.winfo_rooty()+a.joint_controls.winfo_height())
                    ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())).save(ROOT/f'verification/episode-adjust-sync/actual-step-{size}.png')
                    a.open_episode_adjust();root.update()
                    ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())).save(ROOT/f'verification/episode-adjust-sync/actual-settings-{size}.png')
                frame=cv2.imread(str(ROOT/'tests/fixtures/insert-normal/camera-0.png'));data=a.detector.process(frame)
                a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
                a.show_page('camera');a.camera_all.set(True);root.update();a.draw_camera_frame(time.monotonic());root.update()
                ImageGrab.grab(bbox=(root.winfo_rootx(),root.winfo_rooty(),root.winfo_rootx()+root.winfo_width(),root.winfo_rooty()+root.winfo_height())).save(ROOT/'verification/episode-adjust-sync/camera-1180x760.png')
            finally:
                if a.camera:a.camera.running=False
                a.close()
