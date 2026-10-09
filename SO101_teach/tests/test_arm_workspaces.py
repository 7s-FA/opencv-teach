import json,shutil,tempfile,time,threading,unittest,tkinter as tk
from pathlib import Path
from copy import deepcopy
from unittest.mock import Mock,patch
from types import SimpleNamespace
from so101_teach.domain import ROOT,Snapshot,JOINTS
from so101_teach.motion import MotionSession
from so101_teach.arm_workspaces import ArmWorkspaces

class ArmWorkspacesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        for name in ('profile.json','robot_profiles.json','jigs.json','workcell-preview.json','pi-connection.json'):shutil.copy2(ROOT/'data'/name,self.data/name)
        shutil.copytree(ROOT/'data/calibration',self.data/'calibration');shutil.copytree(ROOT/'data/episodes',self.data/'episodes')
        (self.data/'preferences.json').write_text(json.dumps({'device_host':'local'}))
        self.root=tk.Tk();self.root.withdraw();self.manager=ArmWorkspaces(self.root,self.data,render=False,auto_camera=False,auto_devices=False)
        self.a=self.manager.apps['arm2'];self.b=self.manager.apps['arm3'];self.manager.select('arm2');self.a.root.deiconify();self.root.update()
    def tearDown(self):
        for a in self.manager.apps.values():
            if a.session:a.session.running=False
            if a.camera:a.camera.running=False
            if a.leader_session:a.leader_session.running=False
        self.manager.finish_safe_close();self.temp.cleanup()
    def connected(self,a,state='HOLD'):
        s=MotionSession(a.profile['port'],a.calibration);s.running=True;s.state=state;s.request=Mock(return_value=7);s.close=Mock(side_effect=lambda:setattr(s,'running',False))
        s.latest=Snapshot('follower',a.target.copy(),{n:{'torque':1 if state!='READ_ONLY' else 0} for n in JOINTS},time.monotonic(),time.time(),a.calibration.sha256,True,a.profile['port']);a.latest=s.latest;a.session=s
        if state=='MOVING':s.program_active.set();s.index=3
        return s
    def test_fast_selection_keeps_torque_program_and_both_connections(self):
        sa=self.connected(self.a,'MOVING');sb=self.connected(self.b,'HOLD');episode=deepcopy(self.a.episode);times=[]
        with patch.object(self.a,'connect_devices') as ca,patch.object(self.b,'connect_devices') as cb:
            for key in ('arm3','arm2','arm3','arm2'):
                began=time.monotonic();self.manager.select(key);times.append(time.monotonic()-began);self.root.update()
            ca.assert_not_called();cb.assert_not_called()
        self.assertTrue(sa.running);self.assertTrue(sb.running);self.assertTrue(sa.program_active.is_set());self.assertEqual(sa.index,3);self.assertEqual(sa.state,'MOVING');self.assertEqual(sb.state,'HOLD')
        sa.close.assert_not_called();sb.close.assert_not_called();sa.request.assert_not_called();sb.request.assert_not_called();self.assertEqual(self.a.episode,episode)
        self.assertLess(max(times),.5,times)
    def test_new_command_only_reaches_selected_workspace(self):
        sa=self.connected(self.a);sb=self.connected(self.b);self.manager.select('arm3');self.b.motion_request('move',[self.b.target])
        sa.request.assert_not_called();sb.request.assert_called_once_with('move',[self.b.target])
    def test_background_poll_keeps_heartbeat_and_pending_safe_entry(self):
        sa=self.connected(self.a,'MOVING');self.connected(self.b);self.a.safe_entry={'request_id':7,'steps':[{'id':'remaining'}],'page':'teach'}
        self.manager.select('arm3');self.assertIsNotNone(self.a.safe_entry);sa.completed_request_id=7;sa.state='HOLD';sa.program_active.clear();sa.heartbeat=0
        with patch.object(self.a,'prepare_execution') as prepare:
            self.a.root.after_cancel(self.a.job);self.a.poll();prepare.assert_called_once_with('play',[{'id':'remaining'}])
        self.assertGreater(sa.heartbeat,0);self.assertEqual(self.manager.active,'arm3');self.assertEqual(self.a.root.state(),'withdrawn')
    def test_common_camera_waits_for_other_arm_measurement_without_resetting_it(self):
        c=SimpleNamespace(running=True,close=Mock());self.a.camera=c;self.a.camera_task={'kind':'jig'}
        self.assertFalse(self.manager.claim_camera(self.b));c.close.assert_not_called()
        self.a.camera_task=None;c.close.side_effect=lambda:setattr(c,'running',False)
        self.assertTrue(self.manager.claim_camera(self.b));c.close.assert_called_once()
    def test_waiting_measurement_does_not_expire_before_camera_is_available(self):
        self.b.camera_waiting_for_arm=True;self.b.pending_execution={'started':time.monotonic()-100,'budget_started':time.monotonic()-100}
        self.b.check_pending_execution();self.assertIsNotNone(self.b.pending_execution);self.b.pending_execution=None
    def test_workspace_state_episode_selection_and_teaching_hold_are_independent(self):
        self.a.jig_updates_paused=True;self.a.teaching_jig_results={'pallet':{'marker':'arm2'}};self.a.selected='arm2-step';self.b.selected='arm3-step'
        self.manager.select('arm3');self.b.jig_updates_paused=False;self.manager.select('arm2')
        self.assertTrue(self.a.jig_updates_paused);self.assertEqual(self.a.teaching_jig_results,{'pallet':{'marker':'arm2'}});self.assertEqual(self.a.selected,'arm2-step');self.assertEqual(self.b.selected,'arm3-step')
    def test_shared_jig_edit_rejected_while_other_arm_runs(self):
        sa=self.connected(self.a,'MOVING')
        with self.assertRaisesRegex(ValueError,'다른 팔'):self.manager.require_shared_jig_idle(self.b)
        self.assertTrue(sa.program_active.is_set());sa.request.assert_not_called()
    def test_hidden_workspace_skips_render_but_remains_running(self):
        self.connected(self.a,'MOVING');self.manager.select('arm3')
        with patch.object(self.a,'draw_camera_frame') as draw:
            self.a.root.after_cancel(self.a.render_job);self.a.render_tick();draw.assert_not_called()
        self.assertFalse(self.a.closed);self.assertTrue(self.a.session.running)
    def test_quick_buttons_reflect_both_arms_states(self):
        self.connected(self.a,'MOVING');self.connected(self.b,'HOLD');self.manager.update_buttons()
        self.assertIn('실행 중',self.b.arm_quick_buttons['arm2'].cget('text'));self.assertIn('유지',self.a.arm_quick_buttons['arm3'].cget('text'))
    def test_close_waits_for_safe_shutdown_before_disconnect(self):
        sa=self.connected(self.a);sb=self.connected(self.b)
        with patch('so101_teach.safe_shutdown.SafeShutdown.start') as start:
            self.manager.close();start.assert_called_once()
        sa.close.assert_not_called();sb.close.assert_not_called()
        self.manager.finish_safe_close();sa.close.assert_called_once();sb.close.assert_called_once()
        self.assertEqual(self.manager.finished,{'arm2','arm3'})

    def test_profile_changes_survive_switch_and_episode_memory_is_per_arm(self):
        self.a.profile['test_committed_setting']='kept';self.manager.select('arm3')
        saved=json.loads((self.data/'robot_profiles.json').read_text())
        self.assertEqual(next(p for p in saved.values() if p.get('robot_id')=='arm2')['test_committed_setting'],'kept')
        self.a.save_preferences();self.b.save_preferences();entries=self.a.preferences.get('last_episode_by_arm',{})
        for app in (self.a,self.b):
            if app.episode['steps']:self.assertEqual(entries[app.profile['robot_id']],app.episode['id'])
    def test_photo_records_are_restored_for_each_preloaded_workspace(self):
        for app in (self.a,self.b):
            self.assertEqual(len(app.settings.photo_paths),35)
            self.assertTrue(all(p.is_file() for p in app.settings.photo_paths))

    def test_selection_keeps_current_tab_and_view_in_both_directions(self):
        self.b.show_page('settings');self.b.set_mode('live')
        self.a.show_page('devices');self.a.set_mode('target');self.a.view=(15.,-40.,1.1,0.,0.,0.)
        self.manager.select('arm3')
        self.assertEqual(self.b.page,'devices');self.assertEqual(self.b.mode,'target');self.assertEqual(self.b.view,self.a.view)
        self.b.show_page('camera');self.manager.select('arm2');self.assertEqual(self.a.page,'camera')
