"""Rubber pads follow each finger without changing the teaching contract."""
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import mujoco
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.preview import configured_scene
from so101_teach.gripper_pads import PAD_SOURCE,append_gripper_pads
from so101_teach.workcell_preview import load_placement

class GripperPads(unittest.TestCase):
    def placement(self):
        p=load_placement(ROOT/'data');p['other_arm_dynamic']=True;return p

    def test_all_three_arms_reuse_the_original_pad_meshes_and_placements(self):
        scene=ET.fromstring(configured_scene([],workcell=self.placement(),full_map=True))
        source=ET.parse(PAD_SOURCE).getroot()
        for prefix in ('','arm3_preview_','arm1_'):
            for kind,body in (('fixed','gripper_link'),('moving','moving_jaw_so101_v1_link')):
                name='razer_'+kind+'_pad';visual=source.find(f".//visual[@name='{name}']")
                geom=scene.find(f".//body[@name='{prefix+body}']/geom[@name='{prefix+name}']")
                self.assertIsNotNone(geom)
                self.assertEqual(geom.get('material'),'gripper_rubber')
                np.testing.assert_allclose(np.fromstring(geom.get('pos'),sep=' '),np.fromstring(visual.find('origin').get('xyz'),sep=' '),atol=1e-12)
                asset=scene.find(f"asset/mesh[@name='{geom.get('mesh')}']")
                self.assertEqual(Path(asset.get('file')).resolve(),(PAD_SOURCE.parent/visual.find('geometry/mesh').get('filename')).resolve())
                self.assertEqual(asset.get('scale'),'1 1 1')
        append_gripper_pads(scene)
        self.assertEqual(len([g for g in scene.iter('geom') if 'razer_' in g.get('name','')]),6)

    def test_moving_pads_follow_jaw_joints_without_changing_mass_or_ranges(self):
        p=self.placement()
        with patch('so101_teach.gripper_pads.append_gripper_pads'):
            baseline=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p))
        m=mujoco.MjModel.from_xml_string(configured_scene([],workcell=p));d=mujoco.MjData(m)
        for field in ('body_mass','body_inertia','jnt_range','actuator_gear'):
            np.testing.assert_array_equal(getattr(m,field),getattr(baseline,field))
        for prefix in ('','arm3_preview_'):
            fixed=prefix+'razer_fixed_pad';moving=prefix+'razer_moving_pad'
            joint=m.joint(prefix+'gripper');d.qpos[joint.qposadr[0]]=0;mujoco.mj_forward(m,d)
            fixed_before=d.geom(fixed).xpos.copy();moving_before=d.geom(moving).xpos.copy()
            d.qpos[joint.qposadr[0]]=.5;mujoco.mj_forward(m,d)
            np.testing.assert_allclose(d.geom(fixed).xpos,fixed_before,atol=1e-12)
            self.assertGreater(np.linalg.norm(d.geom(moving).xpos-moving_before),.001)
            self.assertEqual(m.geom(moving).contype,0);self.assertEqual(m.geom(moving).conaffinity,0)

if __name__=='__main__':unittest.main()
