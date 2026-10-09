import json,unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
from PIL import Image
from tests import test_ui as fixtures

class CalibrationPhotoRestoreTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def saved(self):
        a=self.app;s=a.settings;folder=self.data/'used-photos';folder.mkdir()
        for n in ('used-a.png','used-b.png','not-used.png'):Image.new('RGB',(64,48),'white').save(folder/n)
        d=deepcopy(a.profile['intrinsics']);d.update(source_directory=str(folder),source_files=['used-a.png','used-b.png'],errors_px=[.12,.34],rms_px=.25,board={'cols':13,'rows':9,'square_mm':20.})
        a.profile['intrinsics']=d;return s,folder
    def test_restores_only_used_images_folder_board_and_original_errors_without_recalculation(self):
        s,folder=self.saved()
        with patch('cv2.calibrateCamera',side_effect=AssertionError('must not recalibrate')):s.restore_calibration_photos()
        self.assertEqual(s.value('photo_dir'),str(folder));self.assertEqual([p.name for p in s.photo_paths],['used-a.png','used-b.png'])
        self.assertEqual(s.photos.item('0','values'),('used-a.png','0.120'));self.assertIn('RMS 0.250px',s.camera_info.get());self.assertIn('사용 사진 2장',s.camera_info.get());self.assertEqual(s.board(),(13,9,20.))
    def test_missing_source_remains_visible_and_does_not_remove_calibration(self):
        s,folder=self.saved();(folder/'used-b.png').unlink();before=deepcopy(self.app.profile['intrinsics']);s.restore_calibration_photos()
        self.assertEqual(len(s.photo_paths),2);self.assertIn('파일 없음',s.photos.item('1','values')[0]);self.assertIn('파일 확인 1장',s.camera_info.get());self.assertEqual(self.app.profile['intrinsics'],before)
    def test_absolute_sources_infer_original_folder(self):
        s,folder=self.saved();d=self.app.profile['intrinsics'];d.pop('source_directory');d['source_files']=[str(folder/'used-a.png')]
        s.restore_calibration_photos();self.assertEqual(s.value('photo_dir'),str(folder));self.assertEqual(s.photo_paths,[folder/'used-a.png'])
    def test_valid_import_without_sources_is_not_reported_uncalibrated(self):
        d=self.app.profile['intrinsics'];d.pop('source_files',None);d.pop('source_directory',None);self.app.settings.restore_calibration_photos()
        self.assertIn('보정 적용됨',self.app.settings.camera_info.get());self.assertIn('사진 기록 없음',self.app.settings.camera_info.get())
    def test_double_click_preview_opens_original_in_same_settings_notebook(self):
        s,folder=self.saved();s.restore_calibration_photos();before=len(s.tabs.tabs());s.photos.selection_set('0');s.preview_calibration_photo()
        self.assertEqual(len(s.tabs.tabs()),before+1);self.assertEqual(s.tabs.tab(s.tabs.select(),'text'),'보정 사진')
    def test_import_restores_provenance_and_errors(self):
        s,folder=self.saved();path=self.data/'lens.json';path.write_text(json.dumps(self.app.profile['intrinsics']));s.vars['intrinsics_file'].set(str(path));s.photo_paths=[];s.import_intrinsics()
        self.assertEqual(len(s.photo_paths),2);self.assertEqual(s.photos.item('1','values')[1],'0.340')
