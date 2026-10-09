import json,tempfile,unittest
from pathlib import Path
import numpy as np
from scipy.spatial.transform import Rotation
from so101_teach.domain import ROOT,JOINTS
from so101_teach.preview import configured_scene
from so101_teach.workcell_preview import load_placement

class WorkcellPreviewTests(unittest.TestCase):
    def test_absent_placement_preserves_single_arm_and_invalid_numbers_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(load_placement(folder))
            value={'schema':1,'display_only':True,'robot_id':'arm3','source':'camera_visual_estimate','base_xyz_mm':[0,0,float('nan')],'joint_angles_rad':[0]*6,'base_yaw_deg':0}
            Path(folder,'workcell-preview.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError):load_placement(folder)
    def test_static_replica_matches_articulated_fk_without_adding_control_joints(self):
        import mujoco,xml.etree.ElementTree as ET
        cfg={'base_xyz_mm':[242,466,-5],'base_yaw_deg':-106.,'joint_angles_rad':[.2,-.1,-1.,.5,.05,.1]}
        original=mujoco.MjModel.from_xml_string(configured_scene([]));model=mujoco.MjModel.from_xml_string(configured_scene([],workcell=cfg));data=mujoco.MjData(model)
        self.assertEqual(model.nq,original.nq);self.assertEqual(model.nu,original.nu);self.assertEqual(model.njnt,6)
        for n,a in zip(JOINTS,cfg['joint_angles_rad']):data.qpos[model.joint(n).qposadr[0]]=a
        mujoco.mj_forward(model,data);rot=Rotation.from_euler('z',cfg['base_yaw_deg'],degrees=True).as_matrix();shift=np.array(cfg['base_xyz_mm'])/1000
        for name in ('base_link','shoulder_link','upper_arm_link','lower_arm_link','wrist_link','gripper_link','gripper_frame_link'):
            np.testing.assert_allclose(data.body('arm3_preview_'+name).xpos,rot@data.body(name).xpos+shift,atol=1e-8)
        frozen=data.body('arm3_preview_gripper_frame_link').xpos.copy();data.qpos[:]=0;mujoco.mj_forward(model,data)
        np.testing.assert_allclose(data.body('arm3_preview_gripper_frame_link').xpos,frozen,atol=1e-12)
        scene=ET.fromstring(configured_scene([],workcell=cfg));replica=scene.find(".//body[@name='arm3_preview']")
        self.assertFalse(list(replica.iter('joint')))
        self.assertFalse(any('overhead' in str(n.attrib) for n in replica.iter()))
        self.assertEqual(len(scene.findall(".//geom[@name='overhead_cam_mount_bottom']")),1)
        self.assertTrue(all(g.get('contype')=='0' and g.get('conaffinity')=='0' for g in replica.iter('geom')))
