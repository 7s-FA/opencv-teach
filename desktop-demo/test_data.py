import json,shutil,tempfile,unittest
from pathlib import Path
from data import prepare_data
SOURCE=Path(__file__).resolve().parents[1]/'SO101_teach'

class DataTest(unittest.TestCase):
    def test_motor_mode_is_forced_offline_without_losing_manual_tcp_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=prepare_data(SOURCE,Path(temp)/'한글')
            file=folder/'profile.json';profile=json.loads(file.read_text())
            self.assertEqual(profile['tcp']['mode'],'model')
            profile['mode']='follower';profile['tcp']['mode']='manual';profile['tcp']['xyz_mm']=[1,2,3]
            file.write_text(json.dumps(profile));prepare_data(SOURCE,folder)
            saved=json.loads(file.read_text())
            self.assertEqual(saved['mode'],'demo');self.assertEqual(saved['tcp']['mode'],'manual');self.assertEqual(saved['tcp']['xyz_mm'],[1,2,3])
    def test_moving_the_workspace_keeps_edits_and_relocates_model_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            old=prepare_data(SOURCE,Path(temp)/'original');(old/'episodes/user.txt').write_text('saved')
            new=Path(temp)/'moved';shutil.move(old,new);prepare_data(SOURCE,new)
            self.assertEqual((new/'episodes/user.txt').read_text(),'saved')
            self.assertTrue(all(Path(v['stl']).exists() for v in json.loads((new/'jigs.json').read_text()).values()))

if __name__=='__main__':unittest.main()
