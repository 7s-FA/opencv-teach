import importlib.util,json,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('native_server',Path(__file__).with_name('server.py'));server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
class Preparation(unittest.TestCase):
    def test_session_data_is_offline_and_original_is_untouched(self):
        source=server.SOURCE
        seed=source/'examples/data'
        original=(seed/'profile.json').read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            before=server.RUN;server.RUN=Path(folder)
            try:
                target=server.prepare_app()
                p=json.loads((target/'data/profile.json').read_text())
                self.assertEqual(p['mode'],'demo')
                self.assertEqual(p['port'],'DEMO')
                self.assertEqual(json.loads((target/'data/preferences.json').read_text())['device_host'],'local')
                self.assertFalse((target/'data/pi-connection.json').exists())
                self.assertTrue((target/'calibration/mount_geometry/source/cam_mount_bottom.stl').exists())
                self.assertEqual((seed/'profile.json').read_bytes(),original)
                self.assertEqual((target/'so101_teach/ui.py').read_bytes(),(source/'so101_teach/ui.py').read_bytes())
            finally:server.RUN=before
if __name__=='__main__':unittest.main()
