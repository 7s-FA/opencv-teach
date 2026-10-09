import unittest,tempfile,time
from pathlib import Path
import numpy as np
import mujoco
from so101_teach.domain import ROOT
from so101_teach.configuration import model_tcp
from so101_teach.inspection import build_scene,read_triangles,binary_stl

class InspectionGeometryTests(unittest.TestCase):
    def test_tcp_markers_use_actual_gripper_frame_transform(self):
        spec={'kind':'tcp','tcp':model_tcp()};xml,assets,marks,info=build_scene(spec)
        m=mujoco.MjModel.from_xml_string(xml,assets=assets);d=mujoco.MjData(m);mujoco.mj_forward(m,d)
        frame=d.body('gripper_frame_link')
        np.testing.assert_allclose(marks['origin'],frame.xpos,atol=1e-9)
        np.testing.assert_allclose(marks['tcp'],frame.xpos+frame.xmat.reshape(3,3)@np.array(spec['tcp']['xyz_mm'])/1000,atol=1e-9)
        self.assertAlmostEqual(info['offset_mm'],6.8122054597887205)
        self.assertEqual(m.nu,0);self.assertEqual(m.njnt,0)
    def test_translated_stl_is_centered_and_units_are_applied(self):
        tri=read_triangles(ROOT/'assets/jigs/pallet.stl')+np.array([120.,-73.,42.])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'moved.stl';path.write_bytes(binary_stl(tri))
            xml,assets,marks,info=build_scene({'kind':'jig','stl':str(path),'unit':'cm'})
            np.testing.assert_allclose(info['size_mm'],[700,700,250]);self.assertAlmostEqual(info['rim_mm'],200)
            m=mujoco.MjModel.from_xml_string(xml,assets=assets);d=mujoco.MjData(m);mujoco.mj_forward(m,d)
            gid=0;mesh=m.geom_dataid[gid];start=m.mesh_vertadr[mesh];count=m.mesh_vertnum[mesh]
            vertices=m.mesh_vert[start:start+count]@d.geom_xmat[gid].reshape(3,3).T+d.geom_xpos[gid]
            np.testing.assert_allclose(vertices.min(0),[-.35,-.35,0],atol=1e-6)
            np.testing.assert_allclose(vertices.max(0),[.35,.35,.25],atol=1e-6)
            self.assertEqual(m.nu,0)
    def test_manual_outline_is_explicit_and_invalid_stl_is_rejected(self):
        xml,assets,marks,info=build_scene({'kind':'jig','unit':'mm','stl':None,'size_mm':[80,60],'rim_mm':10})
        self.assertFalse(info['has_stl']);self.assertEqual(info['size_mm'],[80,60,10])
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'bad.stl';p.write_text('not a mesh')
            with self.assertRaises(ValueError):build_scene({'kind':'jig','unit':'mm','stl':str(p)})
