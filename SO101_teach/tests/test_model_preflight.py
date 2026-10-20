import hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from so101_teach.model_regions import identity,verify_profile_models

class ModelPreflightTests(unittest.TestCase):
    def fixture(self,root):
        (root/'model.bin').write_bytes(b'weights')
        manifest={'files':{'model.bin':hashlib.sha256(b'weights').hexdigest()},'_provider_sha256':{'worker':'new'}}
        settings={'method':'model_roi','model':'n','confidence':.5,'model_identity':identity(manifest)}
        return manifest,settings
    def test_opencv_needs_no_model_runtime(self):
        with patch('so101_teach.model_regions.runtime') as runtime:
            verify_profile_models({},inspect_parts=True)
        runtime.assert_not_called()
    def test_valid_shared_model_is_checked_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest,settings=self.fixture(root)
            with patch('so101_teach.model_regions.runtime',return_value=({},root,manifest)) as runtime:
                verify_profile_models({'jig_detection':settings,'part_inspection':settings},inspect_parts=True)
            self.assertEqual(runtime.call_count,1)
    def test_stale_selection_is_rejected_without_changing_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest,settings=self.fixture(root);settings['model_identity']='0'*64
            with patch('so101_teach.model_regions.runtime',return_value=({},root,manifest)):
                with self.assertRaisesRegex(ValueError,'실행 코드가 다릅니다'):verify_profile_models({'jig_detection':settings})
            self.assertEqual(settings['model_identity'],'0'*64)
    def test_changed_weight_and_separate_inspection_selection_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest,settings=self.fixture(root);(root/'model.bin').write_bytes(b'corrupt')
            with patch('so101_teach.model_regions.runtime',return_value=({},root,manifest)):
                with self.assertRaisesRegex(ValueError,'모델 파일 해시'):verify_profile_models({'part_inspection':settings},inspect_parts=True)
