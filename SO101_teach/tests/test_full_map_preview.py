"""Full-room rendering must not alter teaching/inspection reference frames."""
from copy import deepcopy
import unittest
import numpy as np
import mujoco
from so101_teach.domain import ROOT,JOINTS
from so101_teach.preview import configured_scene,apply_other_arm_pose
from so101_teach.full_map import map_from_preview,overview_view
from so101_teach.workcell_preview import load_placement
from so101_teach.configuration import model_tcp

class FullMapPreviewTests(unittest.TestCase):
    def test_registered_pallet_is_not_duplicated_by_the_room_background(self):
        from so101_teach.configuration import JigCatalog
        from so101_teach.domain import load_profile
        profile,_,_=load_profile();catalog=JigCatalog(ROOT/'data',profile)
        mesh=catalog.mesh('pallet');spec={**mesh,'stl':mesh['stl_path']}
        placement=load_placement(ROOT/'data')
        for jigs in ([],[spec]):
            model=mujoco.MjModel.from_xml_string(configured_scene(jigs,model_tcp(),workcell=placement,full_map=True))
            self.assertEqual(mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_GEOM,'map_finished_pallet'),-1)
            if jigs:self.assertGreaterEqual(mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_GEOM,'registered_jig_0'),0)
            for index in (0,1):self.assertGreaterEqual(mujoco.mj_name2id(model,mujoco.mjtObj.mjOBJ_GEOM,f'linear_a_fixture_{index}_visual'),0)

    def build(self,placement,enabled):
        m=mujoco.MjModel.from_xml_string(configured_scene([],model_tcp(),workcell=placement,full_map=enabled))
        d=mujoco.MjData(m)
        for n,q in zip(JOINTS,[.12,-.1,-.8,.35,.1,.08]):d.qpos[m.joint(n).qposadr[0]]=q
        apply_other_arm_pose(m,d,placement);mujoco.mj_forward(m,d);return m,d

    def test_live_arm_frames_and_joint_ranges_are_unchanged_for_both_selected_arms(self):
        for selected in ('arm2','arm3'):
            p=load_placement(ROOT/'data');p.update(active_arm_id=selected,other_arm_dynamic=True,
                other_arm_joint_angles_rad=[-.2,-.3,-.7,.4,.15,.12])
            before=deepcopy(p);a,da=self.build(p,False);b,db=self.build(p,True)
            self.assertEqual(a.njnt,b.njnt);self.assertEqual(a.nu,b.nu)
            for i in range(a.njnt):
                name=a.joint(i).name
                np.testing.assert_allclose(a.joint(name).range,b.joint(name).range)
            for body in ('base_link','gripper_link','gripper_frame_link','arm3_preview_base_link','arm3_preview_gripper_link','linear_carriage'):
                np.testing.assert_allclose(da.body(body).xpos,db.body(body).xpos,atol=1e-12)
                np.testing.assert_allclose(da.body(body).xmat,db.body(body).xmat,atol=1e-12)
            np.testing.assert_allclose(da.site('active_tcp').xpos,db.site('active_tcp').xpos,atol=1e-12)
            self.assertEqual(p,before)

    def test_room_positions_remain_registered_when_the_display_table_datum_changes(self):
        p=load_placement(ROOT/'data');p['board']['top_z_mm']-=5
        m,d=self.build(p,True);R,t=map_from_preview(p)
        for name,target in [('storage_table',[3.6,1.36,0]),('aruco_board_P1',[2.1,1.798,.1105]),
                            ('aruco_board_P2',[1.74,1.798,.1105]),('aruco_board_MAT',[3.1485,1.06,.115]),
                            ('aruco_board_ASM',[1.895,.5158,.115])]:
            np.testing.assert_allclose(R.apply(d.body(name).xpos)+t,target,atol=1e-8)
        np.testing.assert_allclose(R.apply(overview_view(p)[3:])+t,[1.8,.9,.14],atol=1e-12)
        self.assertEqual(m.geom('workcell_physical_floor').group,3)
        self.assertEqual(m.geom('workcell_backdrop').group,3)
        self.assertFalse(any(m.joint(i).name.startswith('arm1_') for i in range(m.njnt)))

    def test_optical_inspection_scene_stays_separate_from_display_only_map(self):
        p=load_placement(ROOT/'data');m,_=self.build(p,False)
        self.assertEqual(mujoco.mj_name2id(m,mujoco.mjtObj.mjOBJ_BODY,'storage_table'),-1)
        m=mujoco.MjModel.from_xml_string(configured_scene([],full_map=True))
        self.assertEqual(mujoco.mj_name2id(m,mujoco.mjtObj.mjOBJ_BODY,'full_map_environment'),-1)

    def test_all_four_perimeter_walls_are_visible_in_the_app_render_options(self):
        m,_=self.build(load_placement(ROOT/'data'),True)
        options=mujoco.MjvOption();options.geomgroup[3]=0;options.geomgroup[4]=1
        for name in ('wall_assembly','wall_storage','wall_opposite_assembly','wall_opposite_storage'):
            wall=m.geom(name)
            self.assertTrue(options.geomgroup[wall.group],name)
            self.assertGreater(m.mat_rgba[wall.matid,3],.99,name)

    def test_ceiling_panels_are_visual_only_and_follow_the_room_transform(self):
        p=load_placement(ROOT/'data');m,d=self.build(p,True);R,t=map_from_preview(p)
        panels=[m.geom(i) for i in range(m.ngeom) if m.geom(i).name.startswith('mujoco_Ceiling_')]
        self.assertEqual(len(panels),12)
        for g in panels:
            self.assertEqual(int(g.contype[0]),0);self.assertEqual(int(g.conaffinity[0]),0)
            self.assertAlmostEqual((R.apply(d.geom(g.name).xpos)+t)[2],2.4)
        floor=m.material('workcell_stone').id
        self.assertGreater(m.mat_reflectance[floor],0)
        self.assertLess(m.mat_reflectance[floor],.1)
        for name in ('robot_and_camera_mount_plastic','arm1_gray_filament','workcell_white_pla'):
            self.assertEqual(m.mat_reflectance[m.material(name).id],0)
        baseline,_=self.build(p,False)
        for i in range(baseline.nbody):
            name=baseline.body(i).name
            if name:np.testing.assert_allclose(m.body(name).mass,baseline.body(i).mass)

if __name__=='__main__':unittest.main()
