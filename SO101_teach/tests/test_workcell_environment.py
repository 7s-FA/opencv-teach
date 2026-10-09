import unittest
from copy import deepcopy
import numpy as np
from scipy.spatial.transform import Rotation
from so101_teach.domain import ROOT
from so101_teach.preview import configured_scene
from so101_teach.workcell_preview import load_placement,placed_tcp_pose
from so101_teach.configuration import model_tcp

class WorkcellEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.cfg=load_placement(ROOT/'data')
    def build(self,cfg):
        import mujoco
        m=mujoco.MjModel.from_xml_string(configured_scene([],model_tcp(),workcell=cfg));d=mujoco.MjData(m);mujoco.mj_forward(m,d);return m,d
    def test_tcp_composes_base_translation_rotation_and_tool_frame_orientation(self):
        for yaw in [-106,0,90]:
            cfg=deepcopy(self.cfg);cfg.update(base_xyz_mm=[120,-220,35],base_yaw_deg=yaw)
            cfg['tcp'].update(xyz_mm=[8,12,21],rpy_deg=[11,-23,38])
            m,d=self.build(cfg);pose=placed_tcp_pose(cfg)
            np.testing.assert_allclose(d.site('arm3_preview_tcp').xpos*1000,pose[:3,3],atol=.002)
            np.testing.assert_allclose(d.site('arm3_preview_tcp').xmat.reshape(3,3),pose[:3,:3],atol=2e-5)
            self.assertEqual(cfg['tcp']['xyz_mm'],[8,12,21]);self.assertEqual(m.njnt,6)
    def test_board_uses_camera_edge_dimensions_and_preserves_top_height(self):
        cfg=deepcopy(self.cfg);cfg['board'].update(size_mm=[514.3,615.7],center_xy_mm=[110,235],top_z_mm=-7.4,visual_thickness_mm=5.)
        m,d=self.build(cfg);size=m.geom('workcell_board_surface').size*2000
        np.testing.assert_allclose(size,[514.3,615.7,5]);self.assertAlmostEqual(d.geom('workcell_board_surface').xpos[2]*1000+size[2]/2,-7.4)
        self.assertEqual(m.geom('preview_floor').group,3)
        np.testing.assert_allclose(d.body('arm3_preview').xpos*1000,cfg['base_xyz_mm'])
    def test_stage_moves_rod_carriage_and_two_fixtures_together_without_control_joints(self):
        cfg=deepcopy(self.cfg);cfg['linear_stage']['stroke_mm']=1.5;m0,d0=self.build(cfg)
        cfg['linear_stage']['stroke_mm']=100;m1,d1=self.build(cfg)
        ends=cfg['linear_stage']['endpoint_reference']
        distance=(ends['forward']['carriage_x_mm']-ends['retracted']['carriage_x_mm'])/1000
        delta=Rotation.from_euler('z',cfg['linear_stage']['yaw_deg'],degrees=True).apply([distance,0,0])
        for name in ('linear_carriage','linear_a_fixture_0','linear_a_fixture_1'):
            np.testing.assert_allclose(d1.body(name).xpos-d0.body(name).xpos,delta,atol=1e-9)
        for name in ('linear_rail_1_0','linear_rail_-1_1','linear_l12_housing','linear_actuator','base_link','arm3_preview'):
            np.testing.assert_allclose(d1.body(name).xpos,d0.body(name).xpos,atol=1e-12)
        self.assertEqual(m1.njnt,6);self.assertEqual(m1.nu,0)
        for m,d in ((m0,d0),(m1,d1)):
            rod=d.body('linear_l12_rod')
            rod_eye=rod.xpos+rod.xmat.reshape(3,3)@np.array([.1525,0,0])
            carrier=d.body('linear_carriage');stage=cfg['linear_stage']
            ear=carrier.xpos+carrier.xmat.reshape(3,3)@np.array([stage['front_pin_x_mm']/1000,0,stage['actuator_axis_z_mm']/1000])
            np.testing.assert_allclose(rod_eye,ear,atol=1e-12)
            housing=m.geom('linear_l12_housing_visual');mid=housing.dataid[0]
            vertices=m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid]+m.mesh_vertnum[mid]]
            world=vertices@d.geom(housing.name).xmat.reshape(3,3).T+d.geom(housing.name).xpos
            self.assertGreaterEqual(world[:,2].min()*1000-stage['position_mm'][2],-1e-4)

    def test_four_leg_assemblies_meet_board_underside_and_floor(self):
        cfg=deepcopy(self.cfg);m,d=self.build(cfg);b=cfg['board']
        board=d.body('workcell_board');R=board.xmat.reshape(3,3)
        for i,xy in enumerate(b['legs']['centers_xy_mm']):
            leg=d.body(f'workcell_leg_{i}');local=R.T@(leg.xpos-board.xpos)*1000
            np.testing.assert_allclose(local,[*xy,-b['visual_thickness_mm']/2-185],atol=1e-5)
            self.assertAlmostEqual(leg.xpos[2]*1000+185,b['top_z_mm']-b['visual_thickness_mm'])
            self.assertAlmostEqual(leg.xpos[2],d.geom('workcell_physical_floor').xpos[2])
            self.assertLessEqual(abs(xy[0])+40,b['size_mm'][0]/2)
            self.assertLessEqual(abs(xy[1])+40,b['size_mm'][1]/2)
        self.assertEqual(m.njnt,6)

    def test_latest_fork_uses_supplied_mesh_and_transverse_pin(self):
        cfg=deepcopy(self.cfg);m,d=self.build(cfg)
        self.assertEqual(cfg['linear_stage']['front_connector_model'],'latest_top_mount')
        pin=d.geom('linear_front_pin_shaft');axis=pin.xmat.reshape(3,3)[:,2]
        stage=d.body('linear_stage');expected=stage.xmat.reshape(3,3)[:,1]
        self.assertAlmostEqual(abs(np.dot(axis,expected)),1.,places=8)
        rear=d.geom('linear_rear_mount_shaft');np.testing.assert_allclose(rear.xmat.reshape(3,3)[:,2],[0,0,1],atol=1e-8)


    def test_white_sides_and_backdrop_share_the_physical_floor(self):
        cfg=deepcopy(self.cfg);m,d=self.build(cfg);board=cfg['board'];floor=board['top_z_mm']-195
        for i in range(4):
            geom=m.geom(f'workcell_side_panel_{i}');z=d.geom(geom.name).xpos[2]*1000;half=geom.size[2]*1000
            self.assertAlmostEqual(z-half,floor);self.assertAlmostEqual(z+half,board['top_z_mm']-10)
        geom=m.geom('workcell_backdrop');z=d.geom(geom.name).xpos[2]*1000;half=geom.size[2]*1000
        self.assertAlmostEqual(z-half,floor);self.assertAlmostEqual(z+half,floor+400)

    def test_burger_follows_carrier_without_adding_control_joints(self):
        from so101_teach.configuration import JigCatalog
        from so101_teach.height_reference import support_bottom_z,detection_plane_z,support_plane_profile
        from so101_teach.domain import load_profile
        import mujoco
        profile,_,_=load_profile();catalog=JigCatalog(ROOT/'data',profile);key=next(k for k,v in catalog.items.items() if v['name']=='운반용 지그')
        item=catalog.items[key];mesh=catalog.mesh(key);installation=item['physical_installation']
        self.assertEqual(item['support_height_mm'],0.)
        spec={**mesh,'stl':item['stl'],'unit':item['unit'],'platform':installation['platform'],'platform_height_mm':195.,'platform_yaw_deg':-90.}
        model=mujoco.MjModel.from_xml_string(configured_scene([spec],model_tcp(),workcell=self.cfg));data=mujoco.MjData(model)
        slot=model.body('registered_jig_0').mocapid[0];data.mocap_pos[slot]=[.2,-.1,support_bottom_z(item,profile)/1000];mujoco.mj_forward(model,data)
        platform=data.body('carrier_burger_0');self.assertAlmostEqual(platform.xpos[2]*1000,profile['table_z_mm']-195)
        original=platform.xpos.copy();data.mocap_pos[slot,:2]+=[.03,.02];mujoco.mj_forward(model,data)
        np.testing.assert_allclose(platform.xpos-original,[.03,.02,0],atol=1e-12)
        self.assertEqual(model.njnt,6);self.assertEqual(model.nu,0)
        self.assertAlmostEqual(detection_plane_z(support_plane_profile(profile,item),mesh)-(profile['table_z_mm']-195),199.8)
