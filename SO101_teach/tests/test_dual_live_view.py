import unittest,time
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch,Mock
import numpy as np
from so101_teach.domain import JOINTS
from so101_teach.preview import configured_scene,apply_other_arm_pose,scene_signature,arm_visibility,apply_arm_visibility
from so101_teach.arm_workspace import tcp_positions
from tests import test_arm_workspaces as fixtures

class DualLiveViewTests(unittest.TestCase):
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    connected=fixtures.ArmWorkspacesTests.connected
    def test_live_uses_each_arms_own_calibration_and_switch_preserves_live_mode(self):
        for a in (self.a,self.b):self.connected(a);a.set_mode('live')
        self.b.target['shoulder_pan']+=100
        p=self.manager.live_workcell(self.a)
        np.testing.assert_allclose(p['other_arm_joint_angles_rad'],self.b.reference.angles(self.b.latest.ticks));self.assertEqual(p['other_arm_pose_kind'],'live')
        self.b.set_mode('target');self.manager.select('arm3');self.assertEqual(self.b.mode,'live')
        p=self.manager.live_workcell(self.b);np.testing.assert_allclose(p['other_arm_joint_angles_rad'],self.a.reference.angles(self.a.latest.ticks))
    def test_stale_disconnected_or_wrong_calibration_is_never_presented_as_live(self):
        self.connected(self.a);self.connected(self.b);self.a.set_mode('live');sample=self.b.latest
        for kind in ('stale','disconnected','mismatch'):
            self.b.session.running=True;self.b.latest=sample
            if kind=='stale':self.b.latest=replace(sample,monotonic=time.monotonic()-2)
            elif kind=='disconnected':self.b.session.running=False
            else:self.b.latest=replace(sample,calibration_matches=False)
            self.b.session.latest=self.b.latest
            p=self.manager.live_workcell(self.a);self.assertFalse(p['other_arm_visible'],kind);self.assertEqual(p['other_arm_pose_kind'],'unavailable')
    def test_step_and_calibration_use_other_arms_saved_safe_pose_not_live_or_draft(self):
        self.connected(self.a);self.connected(self.b)
        ticks=self.b.target.copy();ticks['shoulder_pan']+=30
        self.b.episode['steps']=[{'safe_boundary':'start','ticks':ticks}]
        self.b.target['shoulder_pan']-=60
        self.a.set_mode('target');p=self.manager.live_workcell(self.a)
        np.testing.assert_allclose(p['other_arm_joint_angles_rad'],self.b.reference.angles(ticks));self.assertEqual(p['other_arm_pose_kind'],'safe')
        self.a.set_mode('live');panel=self.a.settings.calibration_panel;self.a.settings.vars['cal_role'].set('팔로워');p=panel.preview_placement()
        np.testing.assert_allclose(p['other_arm_joint_angles_rad'],self.b.reference.angles(ticks));self.assertFalse(p.get('other_arm_dynamic',False))
    def test_missing_safe_pose_keeps_previous_fixed_reference_without_claiming_safe(self):
        self.b.episode['steps']=[];self.a.workcell_preview.pop('safe_pose_fallback_arm',None);self.a.set_mode('target');p=self.manager.live_workcell(self.a)
        self.assertEqual(p['other_arm_pose_kind'],'reference');self.assertEqual(p.get('other_arm_joint_angles_rad'),self.a.workcell_preview.get('other_arm_joint_angles_rad'))
    def test_arm3_fixed_pose_follows_arm2_safe_angles_without_copying_motor_ticks(self):
        self.a.workcell_preview['safe_pose_fallback_arm']={'arm3':'arm2'};self.b.episode['steps']=[]
        safe=next(s for s in self.a.episode['steps'] if s.get('safe_boundary')=='start')
        before=deepcopy((self.a.episode,self.b.episode));self.a.set_mode('target')
        placement=self.manager.live_workcell(self.a)
        np.testing.assert_allclose(placement['other_arm_joint_angles_rad'],self.a.reference.angles(safe['ticks']))
        self.assertEqual(placement['other_arm_pose_kind'],'safe');self.assertEqual(before,(self.a.episode,self.b.episode))
        self.a.set_mode('live');self.connected(self.b)
        live=self.manager.live_workcell(self.a);np.testing.assert_allclose(live['other_arm_joint_angles_rad'],self.b.reference.angles(self.b.latest.ticks))
    def test_live_mujoco_tcp_matches_each_arms_own_calibrated_fk(self):
        import mujoco
        for app in (self.a,self.b):self.connected(app);app.set_mode('live')
        for app in (self.a,self.b):
            p=self.manager.live_workcell(app);m=mujoco.MjModel.from_xml_string(configured_scene([],app.profile['tcp'],workcell=p));d=mujoco.MjData(m)
            for n,angle in zip(JOINTS,app.reference.angles(app.latest.ticks)):d.qpos[m.joint(n).qposadr[0]]=angle
            apply_other_arm_pose(m,d,p);mujoco.mj_forward(m,d)
            expected=tcp_positions(app.kin.fk(app.latest.ticks),app.profile,p);active=app.profile['robot_id'];other='arm3' if active=='arm2' else 'arm2'
            np.testing.assert_allclose(d.site('active_tcp').xpos*1000,expected[active],atol=.002)
            np.testing.assert_allclose(d.site('arm3_preview_tcp').xpos*1000,expected[other],atol=.002)
    def test_other_live_arm_remains_visible_when_selected_arm_is_disconnected(self):
        self.connected(self.b);self.a.session=None;self.a.latest=None;self.a.set_mode('live');self.a.render_enabled=True
        renderer=Mock();renderer.submit.return_value=True;renderer.poll.return_value=None
        with patch('so101_teach.ui.Renderer',return_value=renderer):
            self.a.root.after_cancel(self.a.render_job);self.a.render_tick()
        p=renderer.submit.call_args.args[2]['workcell'];self.assertFalse(p['active_arm_visible']);self.assertTrue(p['other_arm_visible'])
        self.assertIn('팔2 TCP — 현재값 없음',self.a.tcp_label.get());self.assertNotIn('팔3 TCP —',self.a.tcp_label.get())
    def test_connect_all_only_connects_missing_arm_and_never_replays_motion(self):
        old=self.connected(self.a,'MOVING');self.b.remote_mode=False
        with patch.object(self.a,'connect_devices') as first,patch.object(self.b,'connect_devices') as second:self.manager.connect_all()
        first.assert_not_called();second.assert_called_once();old.request.assert_not_called();self.assertTrue(old.program_active.is_set())
    def test_connect_all_remote_preparation_is_async_and_deduplicated(self):
        self.b.remote_mode=True;self.connected(self.a)
        with patch.object(self.b.settings.pi_panel,'connect') as connect:
            self.manager.connect_all();connect.assert_called_once();self.assertTrue(self.b.startup_devices_pending)
            self.b.settings.pi_panel.job=Mock();self.manager.connect_all();self.assertEqual(connect.call_count,1)
        self.b.settings.pi_panel.job=None
    def test_latest_other_tcp_updates_when_selected_arm_does_not_move(self):
        self.connected(self.a);self.connected(self.b);self.a.set_mode('live');self.a.render_enabled=False
        self.a.root.after_cancel(self.a.render_job);self.a.render_tick();before=self.a.tcp_label.get()
        ticks=self.b.latest.ticks.copy();ticks['shoulder_pan']+=100;self.b.latest=replace(self.b.latest,ticks=ticks,monotonic=time.monotonic());self.b.session.latest=self.b.latest
        self.a.root.after_cancel(self.a.render_job);self.a.render_tick();self.assertNotEqual(self.a.tcp_label.get(),before);self.assertNotIn('(예시)',self.a.tcp_label.get())

class DualSceneTests(unittest.TestCase):
    def placement(self,active='arm2'):
        return {'active_arm_id':active,'base_xyz_mm':[242,466,-5],'arm2_base_z_mm':10.,'base_yaw_deg':-106.,'joint_angles_rad':[0]*6,'other_arm_joint_angles_rad':[.2,-.1,-1.,.5,.05,.1],'other_arm_dynamic':True,'other_arm_visible':True}
    def test_dynamic_replica_follows_joint_values_without_actuators_or_scene_recompile(self):
        import mujoco
        for active in ('arm2','arm3'):
            p=self.placement(active);m=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p));d=mujoco.MjData(m)
            self.assertEqual(m.njnt,12);self.assertEqual(m.nu,0)
            for n,a in zip(JOINTS,[.1,.2,.3,.4,.5,.6]):d.qpos[m.joint(n).qposadr[0]]=a
            apply_other_arm_pose(m,d,p);mujoco.mj_forward(m,d);before=d.body('arm3_preview_gripper_frame_link').xpos.copy();main=d.body('gripper_frame_link').xpos.copy()
            config={'jigs':[],'workcell':deepcopy(p)};key=scene_signature(config)
            p['other_arm_joint_angles_rad'][0]+=.4;apply_other_arm_pose(m,d,p);mujoco.mj_forward(m,d)
            self.assertGreater(np.linalg.norm(before-d.body('arm3_preview_gripper_frame_link').xpos),.01);np.testing.assert_allclose(main,d.body('gripper_frame_link').xpos)
            self.assertEqual(key,scene_signature({'jigs':[],'workcell':p}))
    def test_dynamic_geometry_matches_static_pose_for_both_selected_arms(self):
        import mujoco
        for active in ('arm2','arm3'):
            p=self.placement(active);dynamic=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p));dd=mujoco.MjData(dynamic);apply_other_arm_pose(dynamic,dd,p);mujoco.mj_forward(dynamic,dd)
            p['other_arm_dynamic']=False;static=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p));sd=mujoco.MjData(static);mujoco.mj_forward(static,sd)
            for name in ('base_link','upper_arm_link','wrist_link','gripper_frame_link'):
                np.testing.assert_allclose(dd.body('arm3_preview_'+name).xpos,sd.body('arm3_preview_'+name).xpos,atol=1e-9)
    def test_missing_measurement_hides_geometries_and_tcp_then_recovers(self):
        import mujoco
        p=self.placement();m=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p));groups=arm_visibility(m)
        p['other_arm_visible']=False;apply_arm_visibility(m,groups,p)
        geoms,alpha,sites,site_alpha=groups['other'];self.assertTrue(np.all(m.geom_rgba[geoms,3]==0));self.assertTrue(np.all(m.site_rgba[sites,3]==0))
        p['other_arm_visible']=True;apply_arm_visibility(m,groups,p);np.testing.assert_allclose(m.geom_rgba[geoms,3],alpha)
