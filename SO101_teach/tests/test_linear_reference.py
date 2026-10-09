import unittest
from copy import deepcopy
import numpy as np
import mujoco
from so101_teach.domain import ROOT
from so101_teach.preview import configured_scene,center_markers
from so101_teach.workcell_preview import load_placement
from so101_teach.workcell_scene import linear_endpoint_centers_mm


class LinearReferenceTests(unittest.TestCase):
    def build(self,placement):
        model=mujoco.MjModel.from_xml_string(configured_scene([],workcell=placement))
        data=mujoco.MjData(model);mujoco.mj_forward(model,data)
        return model,data

    def test_each_endpoint_matches_far_pallet_cad_center(self):
        placement=load_placement(ROOT/'data')
        for name in ('forward','retracted'):
            placement['linear_stage']['stroke_mm']=placement['linear_stage']['endpoint_reference'][name]['commanded_mm']
            model,data=self.build(placement)
            geom=model.geom('linear_a_fixture_1_visual');mesh=geom.dataid[0]
            vertices=model.mesh_vert[model.mesh_vertadr[mesh]:model.mesh_vertadr[mesh]+model.mesh_vertnum[mesh]]
            world=vertices@data.geom(geom.name).xmat.reshape(3,3).T+data.geom(geom.name).xpos
            center=(world.min(axis=0)+world.max(axis=0))/2
            np.testing.assert_allclose(data.site('linear_reference_'+name).xpos,center,atol=1e-7)
            self.assertEqual(model.njnt,6);self.assertEqual(model.nu,0)

    def test_references_stay_fixed_when_arm_or_linear_state_changes(self):
        placement=load_placement(ROOT/'data');_,initial=self.build(placement)
        expected={n:initial.site('linear_reference_'+n).xpos.copy() for n in ('forward','retracted')}
        for arm,stroke in (('arm2',1.5),('arm3',100),('arm3',50)):
            cfg=deepcopy(placement);cfg['active_arm_id']=arm;cfg['linear_stage']['stroke_mm']=stroke
            _,data=self.build(cfg)
            for name,point in expected.items():np.testing.assert_allclose(data.site('linear_reference_'+name).xpos,point,atol=1e-12)

    def test_no_endpoint_configuration_has_no_invented_references(self):
        self.assertEqual(linear_endpoint_centers_mm({'stroke_mm':50}),{})

    def test_both_pallet_centers_follow_carriage(self):
        cfg=load_placement(ROOT/'data');model,data=self.build(cfg)
        points=center_markers(model,data)
        for i in (1,2):
            fixture=data.body(f'linear_a_fixture_{i-1}')
            np.testing.assert_allclose(points[f'linear_pallet_center_{i}'],fixture.xpos+[0,0,.00425],atol=1e-9)
        self.assertAlmostEqual(np.linalg.norm(points['linear_pallet_center_1']-points['linear_pallet_center_2']),.08)

    def test_jig_center_follows_visible_pose_and_hides_without_pose(self):
        jig={'low_mm':[12,-18,4],'size_mm':[68,80,20],'unit':'mm','mesh_yaw_offset_deg':180}
        model=mujoco.MjModel.from_xml_string(configured_scene([jig]));data=mujoco.MjData(model)
        mujoco.mj_forward(model,data);self.assertEqual(center_markers(model,data),{})
        index=model.body('registered_jig_0').mocapid[0]
        data.mocap_pos[index]=[.1,.2,.03];data.mocap_quat[index]=[.7071067812,0,0,.7071067812]
        mujoco.mj_forward(model,data)
        np.testing.assert_allclose(center_markers(model,data)['registered_jig_0_center'],[.1,.2,.04],atol=1e-9)
        model.geom('registered_jig_0').rgba[3]=0
        self.assertEqual(center_markers(model,data),{})
