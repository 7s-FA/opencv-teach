import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from so101_teach.roi_reference_ui import ROIReferencePanel
from so101_teach.ui import styles


class ROIReferenceUITests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk();styles(self.root);self.root.geometry('980x560')
    def tearDown(self):
        self.root.destroy()
    def test_views_zoom_and_readonly_criteria(self):
        panel = ROIReferencePanel(self.root);panel.pack(fill='both',expand=True);self.root.update()
        self.assertIn('완성품 팔레트', panel.details.get('1.0','end'))
        for index in (1, 2):
            panel.choice.current(index);panel.select();self.root.update()
            self.assertIsNotNone(panel.photo)
            self.assertIn('선행 단계: 중단 추가 PASS 필요', panel.details.get('1.0','end'))
        self.assertIn('가려져 이전 PASS가 필요한 부품: 하단, 중단',panel.details.get('1.0','end'))
        panel.zoom.set('100%');panel.paint();self.root.update()
        self.assertGreater(float(panel.canvas.cget('scrollregion').split()[2]), panel.canvas.winfo_width())
        self.assertEqual(str(panel.details.cget('state')),'disabled')
        self.assertFalse(panel.data['inspection_enabled'])
    def test_missing_reference_keeps_app_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            panel=ROIReferencePanel(self.root,Path(folder));panel.pack();self.root.update()
            self.assertIn('읽지 못했습니다',panel.status.get())
            self.assertEqual(str(panel.choice.cget('state')),'disabled')
    def test_missing_image_preserves_numeric_criteria(self):
        from so101_teach.domain import ROOT
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            shutil.copy(ROOT/'inspection/roi_reference.json',folder)
            panel=ROIReferencePanel(self.root,Path(folder));panel.pack();self.root.update()
            self.assertIsNone(panel.photo)
            self.assertIn('완성품 팔레트',panel.details.get('1.0','end'))
