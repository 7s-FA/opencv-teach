"""Ensure the Windows virtual-files route preserves original model geometry."""
import shutil,tempfile,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'SO101_teach'))
import mujoco,numpy as np
from so101_teach.domain import ROOT
from so101_teach.mujoco_assets import virtual_assets

class AssetsTest(unittest.TestCase):
    def test_includes_and_meshes_survive_korean_directory_without_geometry_changes(self):
        original=mujoco.MjModel.from_xml_path(str(ROOT/'assets/so101/inspection_scene.xml'))
        with tempfile.TemporaryDirectory(prefix='모델 경로 ') as temp:
            folder=Path(temp)/'한글'
            for name in ('assets','calibration'):shutil.copytree(ROOT/name,folder/name)
            file=folder/'assets/so101/inspection_scene.xml';xml,blobs=virtual_assets(file.read_text(),file.parent)
            # Remove the source files: successful compilation must use only VFS bytes.
            shutil.rmtree(folder)
            packed=mujoco.MjModel.from_xml_string(xml,assets=blobs)
            self.assertEqual(original.ngeom,packed.ngeom);self.assertEqual(original.nq,packed.nq)
            for attr in ('mesh_vert','geom_size','body_pos','body_quat','jnt_axis'):
                np.testing.assert_allclose(getattr(original,attr),getattr(packed,attr),atol=0,rtol=0)

if __name__=='__main__':unittest.main()
