import json,shutil,tempfile,time,unittest
from copy import deepcopy
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch
import tkinter as tk
from so101_teach.domain import ROOT
from so101_teach.ui import App
from so101_teach.geometry import corrected_plan


class AutoStepPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();data=Path(self.temp.name)
        for name in ('profile.json','jigs.json'):shutil.copy2(ROOT/'data'/name,data/name)
        shutil.copytree(ROOT/'data/calibration',data/'calibration')
        self.root=tk.Tk();self.app=App(self.root,data,render=False,auto_camera=False,auto_devices=False)
        arm=self.app.profile['robot_id'];prefs=json.loads((ROOT/'data/preferences.json').read_text())
        self.app.episode=json.loads((ROOT/'data/episodes'/(prefs['last_episode_by_arm'][arm]+'.json')).read_text())
        self.step=next(s for s in self.app.episode['steps'] if s.get('jig_id'))
        self.app.selected=self.step['id'];self.app.follow_jig.set(True);self.app.set_step_jig(self.step['jig_id']);self.app.apply_target(self.step['ticks'])
        reference=self.step['jig_reference'];pose=reference['pose']
        self.results={self.step['jig_id']:{'selected':{'metric':{'center_xy_mm':[pose[0]+1.,pose[1]],'yaw_deg':pose[2],'symmetry_deg':reference['symmetry_deg'],'mesh_yaw_offset_deg':reference.get('mesh_yaw_offset_deg',0.)}},'pose_measured_at':time.monotonic()}}

    def tearDown(self):
        job=getattr(self.app,'step_preview_job',None)
        if job:
            try:job[1].result(timeout=20)
            except Exception:pass
        self.app.close();self.temp.cleanup()

    def result(self):
        value=self.app.detected_step_preview(self.app,self.results)
        job=getattr(self.app,'step_preview_job',None)
        if job:job[1].result(timeout=20)
        return self.app.detected_step_preview(self.app,self.results)

    def test_preview_matches_execution_solver_without_editing_or_motion(self):
        a=self.app;before=deepcopy(a.episode);draft=a.target.copy()
        reference=a.jig_result_reference(self.step['jig_id'],self.results[self.step['jig_id']])
        expected=corrected_plan(a.kin,[self.step],reference,lambda key:a.catalog.mesh(key)['sha256'])[0]['ticks']
        with patch.object(a,'motion_request',side_effect=AssertionError('Preview must not move')):
            ticks,note=self.result()
        self.assertEqual(ticks,expected);self.assertNotEqual(ticks,draft)
        self.assertIn('보정 자세',note);self.assertEqual(a.target,draft);self.assertEqual(a.episode,before)
        a.measured_target_active=True;a.taught_jig_display={'target_active':True}
        self.assertTrue(a.automatic_step_preview())
        with patch.object(a.settings.pool,'submit',side_effect=AssertionError('Unchanged input must use cache')):
            self.assertEqual(a.detected_step_preview(a,self.results)[0],expected)

    def test_missing_detection_restores_saved_pose_and_invalidates_old_render(self):
        a=self.app;self.result();context=a.render_context
        a.measured_target_active=True;a.measured_jig_display={'old':{'pose':[1,2,3]}}
        a.taught_jig_display={'target_active':True}
        self.assertEqual(a.editor_scene_references()[self.step['jig_id']]['pose'],self.step['jig_reference']['pose'])
        ticks,note=a.detected_step_preview(a,{})
        self.assertEqual(ticks,self.step['ticks']);self.assertIn('지그 미탐지',note);self.assertGreater(a.render_context,context)

    def test_old_async_result_cannot_be_shown_for_a_changed_detection(self):
        a=self.app;first=Future();second=Future()
        with patch.object(a.settings.pool,'submit',side_effect=[first,second]):
            self.assertIsNone(a.detected_step_preview(a,self.results)[0])
            self.results[self.step['jig_id']]['selected']['metric']['center_xy_mm'][0]+=1
            first.set_result([{'ticks':{**a.target,'wrist_roll':a.target['wrist_roll']+10}}])
            self.assertIsNone(a.detected_step_preview(a,self.results)[0])
            second.set_result([{'ticks':a.target.copy()}])
            self.assertEqual(a.detected_step_preview(a,self.results)[0],a.target)

    def test_unreachable_preview_is_explicit_and_does_not_show_saved_pose(self):
        a=self.app;failed=Future();failed.set_exception(ValueError('목표 도달 불가'))
        with patch.object(a.settings.pool,'submit',return_value=failed):
            a.detected_step_preview(a,self.results);ticks,note=a.detected_step_preview(a,self.results)
        self.assertIsNone(ticks);self.assertIn('목표 도달 불가',note)

    def test_fixed_step_and_computed_playback_are_not_corrected_again(self):
        a=self.app;a.follow_jig.set(False)
        with patch.object(a.settings.pool,'submit',side_effect=AssertionError('Fixed step')):
            self.assertEqual(a.detected_step_preview(a,self.results)[0],a.target)
        a.playing=True;self.assertFalse(a.automatic_step_preview());a.playing=False
        a.preview_jig_references={};self.assertFalse(a.automatic_step_preview())

    def test_second_editor_uses_its_own_ticks_and_reference(self):
        from types import SimpleNamespace
        a=self.app;other=deepcopy(self.step);other['jig_reference']['pose'][0]+=2
        editor=SimpleNamespace(target={**a.target,'gripper':a.target['gripper']+1},follow_jig=a.follow_jig,original=other,selected_jig_id=a.selected_jig_id)
        editor.with_jig=lambda step,original:{**step,'jig_id':other['jig_id'],'jig_reference':deepcopy(original['jig_reference'])}
        future=Future();future.set_result([{'ticks':editor.target}])
        with patch.object(a.settings.pool,'submit',return_value=future) as submit:
            a.detected_step_preview(editor,self.results)
            self.assertEqual(submit.call_args.args[2][0]['jig_reference'],other['jig_reference'])
            self.assertEqual(submit.call_args.args[2][0]['ticks'],editor.target)
            self.assertEqual(a.detected_step_preview(editor,self.results)[0],editor.target)
