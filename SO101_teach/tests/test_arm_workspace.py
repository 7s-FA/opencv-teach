import json
import base64
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np

from so101_teach.domain import ROOT,EpisodeStore,Calibration,JOINTS
from so101_teach.arm_workspace import arm_id,calibration_pending,require_calibrated,progress_path,preview_profile,scene_placement,tcp_positions
from so101_teach.workcell_preview import load_placement,jig_pose_in_world
from so101_teach.preview import configured_scene,overhead_view
from so101_teach.geometry import Kinematics
from so101_teach.remote_server import Runtime


class ArmWorkspaceTests(unittest.TestCase):
    def setUp(self):
        profiles=json.loads((ROOT/'data/robot_profiles.json').read_text())
        self.two=next(p for p in profiles.values() if p.get('robot_id')=='arm2')
        self.three=deepcopy(next(p for p in profiles.values() if p.get('robot_id')=='arm3'));self.three['calibration_status']='pending'
        self.cal=Calibration(ROOT/'data'/self.two['calibration_file'])
        self.placement=load_placement(ROOT/'data')

    def test_episodes_are_separated_even_when_calibration_hashes_match(self):
        with tempfile.TemporaryDirectory() as folder:
            two=EpisodeStore(folder,self.cal,'arm2');three=EpisodeStore(folder,self.cal,'arm3')
            doc=two.new('팔2');two.save(doc)
            self.assertEqual(len(two.entries()),1);self.assertEqual(three.entries(),[])
            with self.assertRaisesRegex(ValueError,'다른 로봇팔'):three.validate(doc)
            del doc['robot_id'];two.validate(doc)
            with self.assertRaisesRegex(ValueError,'다른 로봇팔'):three.validate(doc)

    def test_new_arm_has_distinct_pending_template_and_progress(self):
        self.assertTrue(calibration_pending(self.three));self.assertNotEqual(self.two['calibration_file'],self.three['calibration_file'])
        self.assertNotEqual(Calibration(ROOT/'data'/self.three['calibration_file']).sha256,self.cal.sha256)
        with self.assertRaisesRegex(ValueError,'새 3점 보정'):require_calibrated(self.three)
        self.assertEqual(progress_path('/tmp','follower',self.two).name,'follower.json')
        self.assertEqual(progress_path('/tmp','follower',self.three).name,'follower-arm3.json')
        self.assertEqual(progress_path('/tmp','leader',self.three).name,'leader.json')

    def test_pi_rejects_connect_and_motion_for_pending_arm(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime=Runtime(folder);runtime.profile=deepcopy(self.three)
            try:
                with self.assertRaisesRegex(ValueError,'새 3점 보정'):runtime.call('connect',{'speed':300})
                with self.assertRaisesRegex(ValueError,'새 3점 보정'):runtime.call('command',{'action':'arm'})
                with self.assertRaisesRegex(ValueError,'등록 포트'):runtime.start_calibration({'role':'follower','port':self.two['port']})
                target=Path(folder)/self.three['calibration_file'];target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((ROOT/'data'/self.three['calibration_file']).read_bytes())
                preset={'json':base64.b64encode(target.read_bytes()).decode()}
                with self.assertRaisesRegex(ValueError,'초기 연결용 파일'):runtime.start_calibration({'role':'follower','port':self.three['port'],'preset':preset})
            finally:runtime.close()

    def test_shared_camera_projection_and_world_view_are_identical(self):
        C2=np.array(self.two['extrinsics']['base_from_camera']);C3=np.array(self.three['extrinsics']['base_from_camera']);T=np.array(self.three['world_from_base'])
        point=np.array([200.,170.,12.,1.]);local=np.linalg.inv(T)@point
        np.testing.assert_allclose(np.linalg.inv(C2)@point,np.linalg.inv(C3)@local,atol=1e-9)
        np.testing.assert_allclose(overhead_view(self.two),overhead_view(self.three),atol=1e-8)
        np.testing.assert_allclose(preview_profile(self.three)['extrinsics']['base_from_camera'],C2,atol=1e-9)

    def test_active_arm3_controls_only_arm3_at_the_confirmed_workcell_position(self):
        import mujoco
        cfg=scene_placement(self.placement,self.three,[0.,0.,0.,0.,0.,0.])
        model=mujoco.MjModel.from_xml_string(configured_scene([],self.three['tcp'],workcell=cfg));data=mujoco.MjData(model)
        mujoco.mj_forward(model,data);static_before=data.site('arm3_preview_tcp').xpos.copy()
        angles=[.1,.2,-.3,.2,.1,.2]
        for name,angle in zip(JOINTS,angles):data.qpos[model.joint(name).qposadr[0]]=angle
        mujoco.mj_forward(model,data)
        np.testing.assert_allclose(data.body('base_link').xpos*1000,self.placement['base_xyz_mm'],atol=1e-5)
        np.testing.assert_allclose(data.body('arm3_preview').xpos,[0,0,0],atol=1e-9)
        np.testing.assert_allclose(data.site('arm3_preview_tcp').xpos,static_before,atol=1e-9)
        expected=tcp_positions(Kinematics(None,tcp=self.three['tcp']).fk_angles(angles),self.three,cfg)
        np.testing.assert_allclose(data.site('active_tcp').xpos*1000,expected['arm3'],atol=.003)
        self.assertEqual(model.njnt,6);self.assertEqual(model.nu,0)
        self.assertAlmostEqual(data.body('linear_stage').xpos[2]*1000,self.placement['board']['top_z_mm'])

    def test_arm_local_jig_pose_returns_to_the_common_workcell(self):
        cfg=scene_placement(self.placement,self.three);T=np.array(self.three['world_from_base']);point=np.array([120.,210.,-7.4,1.]);local=np.linalg.inv(T)@point
        xyz,yaw=jig_pose_in_world(cfg,local[:3].tolist(),20-self.placement['base_yaw_deg'])
        np.testing.assert_allclose(xyz,point[:3],atol=1e-8);self.assertAlmostEqual(yaw,20.)

    def test_height_moves_only_selected_arm_and_keeps_jigs_in_common_world(self):
        import mujoco
        from so101_teach.height_reference import profile_with_arm_height,floor_adjustment
        for profile in (self.two,self.three):
            raised=profile_with_arm_height(profile,floor_adjustment(profile)+5)
            before=scene_placement(self.placement,profile);after=scene_placement(self.placement,raised)
            def positions(placement):
                model=mujoco.MjModel.from_xml_string(configured_scene([],profile['tcp'],workcell=placement));data=mujoco.MjData(model);mujoco.mj_forward(model,data)
                return data.body('base_link').xpos.copy()*1000,data.body('arm3_preview').xpos.copy()*1000
            old_active,old_other=positions(before);new_active,new_other=positions(after)
            np.testing.assert_allclose(new_active-old_active,[0,0,5],atol=.00001)
            np.testing.assert_allclose(new_other,old_other,atol=.00001)
            old_jig,_=jig_pose_in_world(before,[100,200,profile['table_z_mm']],0)
            new_jig,_=jig_pose_in_world(after,[100,200,raised['table_z_mm']],0)
            np.testing.assert_allclose(new_jig,old_jig,atol=.00001)


if __name__=='__main__':unittest.main()
