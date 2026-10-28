from copy import deepcopy
import unittest
from unittest.mock import patch
from PIL import ImageGrab
from tests import test_ui
from so101_teach.domain import ROOT

class ConfidencePercentUITests(unittest.TestCase):
 setUp=test_ui.UITests.setUp
 tearDown=test_ui.UITests.tearDown
 def test_integer_fields_persist_independently_and_reject_invalid_range(self):
  a=self.app;p=a.camera_inspection;a.jig_detection_method.set('AI 마스크 + 지그별 정합');a.update_jig_detection_controls();p.method.set('AI 라벨 + CAD');p.update_method_controls()
  self.assertEqual(a.jig_detection_confidence.get(),'50');self.assertEqual(p.confidence.get(),'50')
  for entry in [a.jig_confidence_choice,p.confidence_entry]:
   entry.delete(0,'end');entry.insert(0,'75.5');self.assertEqual(entry.get(),'')
   entry.insert(0,'abc');self.assertEqual(entry.get(),'');entry.insert(0,'75')
  with patch('so101_teach.model_regions.runtime',return_value=({},None,{'files':{'model.ncnn.bin':'a'*64}})):
   for number in [50,90,75]:
    a.jig_detection_confidence.set(str(number));a.save_jig_detection();self.assertEqual(a.profile['jig_detection']['confidence'],number/100)
   p.method.set('AI 라벨 + CAD');p.update_method_controls();p.confidence.set('76');p.apply_method();self.assertEqual(a.profile['part_inspection']['confidence'],.76);self.assertEqual(a.profile['jig_detection']['confidence'],.75)
   for var,apply in [(a.jig_detection_confidence,a.save_jig_detection),(p.confidence,p.apply_method)]:
    for bad in ['49','91','']:
     before=deepcopy(a.profile);var.set(bad)
     with self.assertRaisesRegex(ValueError,'정수'):apply()
     self.assertEqual(a.profile,before)
    var.set('75')
   p.apply_method();self.assertIn('75%',p.method_status.get());self.assertIn('75%',a.jig_detection_applied_text())

 def test_maximized_input_controls_render_and_accept_enter(self):
  a=self.app;self.root.geometry('1920x1080');a.show_page('camera');a.jig_detection_method.set('AI 마스크 + 지그별 정합');a.update_jig_detection_controls()
  p=a.camera_inspection;p.method.set('AI 라벨 + CAD');p.update_method_controls()
  output=ROOT/'verification/confidence-50-restore-20261022';output.mkdir(exist_ok=True)
  for name,panel,field in [('jig',0,a.jig_confidence_choice),('parts',p,p.confidence_entry),('yolo',a.camera_yolo,a.camera_yolo.threshold)]:
   if name=='parts':p.method.set('AI 라벨 + CAD');p.update_method_controls()
   a.camera_tabs.select(panel);self.root.update();field.focus_force();self.root.update()
   field.delete(0,'end');field.insert(0,'50')
   with patch('so101_teach.model_regions.runtime',return_value=({},None,{'files':{'model.ncnn.bin':'a'*64}})):
    field.event_generate('<Return>');self.root.update()
   self.assertEqual(field.get(),'50');self.assertTrue(field.winfo_ismapped());self.assertLessEqual(field.winfo_rootx()+field.winfo_width(),1920)
   if name=='parts':self.assertIn('50%',p.method_status.get())
   if name=='yolo':self.assertIn('50% 적용',a.camera_yolo.status.get())
   ImageGrab.grab(bbox=(0,0,1920,1080)).save(output/('ui-'+name+'.png'))
  self.assertEqual(a.profile['jig_detection']['confidence'],.5);self.assertEqual(a.profile['part_inspection']['confidence'],.5)
