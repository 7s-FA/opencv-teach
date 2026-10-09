import unittest,json
from pathlib import Path
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import project_plane,world_xy

class LeftCameraMountTests(unittest.TestCase):
    def setUp(self):
        self.folder=ROOT/'calibration/mount_geometry';self.left=json.loads((self.folder/'left_nominal_pose.json').read_text());self.right=json.loads((self.folder/'right_nominal_pose.json').read_text());self.profile=json.loads((ROOT/'assets/reference/camera.json').read_text())
    def test_same_stock_parts_opposite_socket_preserves_robot_and_height(self):
        old=self.right;new=self.left;pitch=new['opposite_socket_pitch_mm'];self.assertAlmostEqual(pitch,232.06414794921875)
        A=np.array(old['assembly_to_forward_left_up'])
        old_base=A@old['assembly_translations_original_stl']['arm_base']-old['robot_base_link_in_assembly']
        new_base=A@new['assembly_translations_original_stl']['arm_base']-new['robot_base_link_in_assembly']
        np.testing.assert_allclose(old_base,new_base,atol=1e-9)
        np.testing.assert_allclose(np.array(new['camera_board_center_in_base_link'])-old['camera_board_center_in_base_link'],[0,pitch,0])
        self.assertGreater(new['camera_board_center_in_base_link'][1],0)
    def test_preview_and_inspection_optical_frames_match_detection(self):
        import mujoco
        expected=np.array(self.profile['extrinsics']['base_from_camera'])
        for name in ('preview_scene.xml','inspection_scene.xml'):
            m=mujoco.MjModel.from_xml_path(str(ROOT/'assets/so101'/name));d=mujoco.MjData(m);mujoco.mj_forward(m,d)
            site=d.site('overhead_optical_center')
            np.testing.assert_allclose(site.xpos*1000,expected[:3,3],atol=1e-6)
            np.testing.assert_allclose(site.xmat.reshape(3,3),expected[:3,:3],atol=1e-6)
            self.assertGreater(d.geom('overhead_cam_mount_middle').xpos[1],0)
    def test_camera_projection_translation_preserves_pixels_and_rotation(self):
        import copy
        new=self.profile;old=copy.deepcopy(new);pitch=self.left['opposite_socket_pitch_mm'];old['extrinsics']['base_from_camera'][1][3]-=pitch
        points=np.array([[150,-100],[200,-70],[250,-120]],float);moved=points+[0,pitch]
        a=project_plane(points,old,12.6);b=project_plane(moved,new,12.6)
        np.testing.assert_allclose(a,b,atol=1e-4);np.testing.assert_allclose(world_xy(b,new,12.6),moved,atol=.02)
        R=np.array(new['extrinsics']['base_from_camera'])[:3,:3];self.assertAlmostEqual(np.linalg.det(R),1)
        self.assertFalse(new['extrinsics']['verified_for_robot_motion'])
