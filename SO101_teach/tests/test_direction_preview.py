import json,tempfile,unittest
from pathlib import Path
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.vision import stl_profile
from so101_teach.preview import configured_scene

class DirectionPreviewTests(unittest.TestCase):
    def test_historical_reference_uses_its_original_model_without_changing_saved_pose(self):
        with tempfile.TemporaryDirectory() as tmp:
            cat=JigCatalog(tmp);old=cat.save({'id':'carrier','name':'carrier','shape':'rectangle','method':'grid','unit':'mm','stl':str(ROOT/'data/jig_assets/959603f107b9287a4e59c7a7108fac58d0f95c1df742ddcd4b31bd9aa07b8172.stl')})
            ref={'stl_sha256':old['mesh']['sha256'],'pose':[100,200,29.],'symmetry_deg':180}
            cat.save({**old,'stl':str(ROOT/'assets/carrier_assembly/carrier_assembly.stl')})
            historic=cat.mesh_for_reference('carrier',ref);self.assertEqual(historic['size_mm'][2],6.);self.assertEqual(historic['sha256'],ref['stl_sha256']);self.assertEqual(ref['pose'],[100,200,29.])
            self.assertIsNone(cat.mesh_for_reference('carrier',{'stl_sha256':'f'*64}))
            new_sha=cat.mesh('carrier')['sha256']
            self.assertIsNone(cat.mesh_for_reference('carrier',{'stl_sha256':new_sha,'symmetry_deg':180}))
            self.assertIsNotNone(cat.mesh_for_reference('carrier',{'stl_sha256':new_sha,'symmetry_deg':360}))
    def test_mujoco_mesh_keeps_the_same_physical_pose_with_legacy_and_teaching_heading_frames(self):
        import mujoco
        path=ROOT/'assets/carrier_assembly/carrier_assembly.stl';mesh=stl_profile(path);physical_yaw=209.4;center=np.array([185.,-134.]);bottom=-2.4;all_vertices=[]
        for yaw,offset in ((209.4,0.),(29.4,180.)):
            cfg={'stl':str(path),'unit':'mm','low_mm':mesh['low_mm'],'size_mm':mesh['size_mm'],'pose':[*center,yaw],'bottom_z_mm':bottom,'mesh_yaw_offset_deg':offset}
            model=mujoco.MjModel.from_xml_string(configured_scene([cfg]));data=mujoco.MjData(model)
            i=model.body('registered_jig_0').mocapid[0];data.mocap_pos[i]=[* (center/1000),bottom/1000];a=np.deg2rad(yaw)/2;data.mocap_quat[i]=[np.cos(a),0,0,np.sin(a)];mujoco.mj_forward(model,data)
            geom=model.geom('registered_jig_0').id;mid=model.geom_dataid[geom];start=model.mesh_vertadr[mid];n=model.mesh_vertnum[mid]
            vertices=model.mesh_vert[start:start+n]@data.geom_xmat[geom].reshape(3,3).T+data.geom_xpos[geom];all_vertices.append(vertices)
            angle=np.deg2rad(physical_yaw);rot=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
            expected=np.r_[np.array([-61.5,-36.])@rot.T+center,bottom+26.5]/1000
            self.assertLess(np.linalg.norm(vertices-expected,axis=1).min(),1e-6)
        np.testing.assert_allclose(all_vertices[0],all_vertices[1],atol=1e-9)
