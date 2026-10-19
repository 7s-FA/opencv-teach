import json,os,time,unittest
from unittest.mock import Mock,patch
from tests import test_ui

class ModelGuidedJigUITests(unittest.TestCase):
 setUp=test_ui.UITests.setUp
 tearDown=test_ui.UITests.tearDown
 def choose(self):
  a=self.app;a.jig_detection_method.set('AI 마스크 + 지그별 정합');a.update_jig_detection_controls()
  return a
 def test_apply_preserves_geometry_clears_old_pose_and_persists_selection(self):
  from copy import deepcopy
  a=self.choose();old=deepcopy(a.profile);a.teaching_jig_results={'pallet':{'selected':{'old':True}}}
  with patch('so101_teach.model_regions.runtime',return_value=({},None,{'files':{'model.ncnn.bin':'a'*64}})):
   a.save_jig_detection()
  self.assertEqual({k:v for k,v in a.profile.items() if k not in ('jig_detection','part_inspection')},old)
  self.assertEqual(a.profile['part_inspection']['method'],'opencv')
  self.assertEqual(a.teaching_jig_results,{});self.assertEqual(a.detector.profile['jig_detection']['method'],'model_roi')
  saved=json.loads((a.data_dir/'profile.json').read_text());self.assertEqual(saved['jig_detection'],a.profile['jig_detection'])
  from so101_teach.remote_config import configuration_bundle
  self.assertEqual(configuration_bundle(a)['profile']['jig_detection'],saved['jig_detection'])
  a.jig_detection_method.set('기존 OpenCV');a.save_jig_detection()
  self.assertEqual(a.profile['jig_detection']['method'],'opencv')
 def test_active_measurement_rejects_switch_without_side_effects(self):
  a=self.choose();a.camera_task={'kind':'jig'};before=a.profile.copy()
  with self.assertRaisesRegex(ValueError,'끝난 뒤'):a.save_jig_detection()
  self.assertEqual(a.profile,before);a.camera_task=None
 def test_model_unavailable_preserves_current_method(self):
  a=self.choose();before=a.profile.copy()
  with patch('so101_teach.model_regions.runtime',side_effect=ValueError('모델 준비 필요')):
   with self.assertRaisesRegex(ValueError,'준비'):a.save_jig_detection()
  self.assertEqual(a.profile,before)
 @unittest.skipUnless(os.environ.get('SO101_JIG_MODEL_RENDER')=='1','Native model-guided jig controls')
 def test_render_new_controls(self):
  from PIL import ImageGrab
  from so101_teach.domain import ROOT
  a=self.choose();a.show_page('camera');a.camera_tabs.select(0);self.root.update()
  x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
  ImageGrab.grab(bbox=(x,y,x+1920,y+1043)).save(ROOT/'verification/cad-only-pallet-20261019/controls.png')
  self.assertEqual(str(a.jig_model_choice.cget('state')),'readonly')
