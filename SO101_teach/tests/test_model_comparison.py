import json,os,tempfile,unittest
from pathlib import Path
import tkinter as tk
from PIL import Image,ImageGrab
from so101_teach.domain import ROOT
from so101_teach.ui import styles
from so101_teach.yolo_ui import ComparisonPanel

class ModelComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)
        self.root=tk.Tk();styles(self.root);self.root.geometry('1920x1043')
    def tearDown(self):self.root.destroy();self.temp.cleanup()
    def test_same_photo_navigation_original_toggle_and_zoom(self):
        rows=[]
        for number in range(2):
            original=f'{number}-original.png';Image.new('RGB',(32,18),'white').save(self.folder/original)
            models={}
            for key in ComparisonPanel.ORDER:
                name=f'{number}-{key}.png';Image.new('RGB',(32,18),'teal').save(self.folder/name)
                models[key]={'image':name,'detections':[],'inference_ms':1,'total_ms':2}
            rows.append({'id':str(number),'original':original,'models':models})
        index=self.folder/'index.json';index.write_text(json.dumps({'status':'complete','images':rows}))
        panel=ComparisonPanel(self.root,index);panel.pack(fill='both',expand=True);self.root.update()
        self.assertEqual(len(panel.canvases),4);self.assertEqual(panel.position,0)
        panel.move(1);self.assertEqual(panel.position,1)
        panel.move(1);self.assertEqual(panel.position,0)
        panel.original.set(True);panel.refresh();self.root.update()
        for image in panel.images.values():self.assertEqual(image.getpixel((0,0)),(255,255,255))
        panel.zoom('sam3');self.root.update()
        self.assertTrue(any(isinstance(child,tk.Toplevel) for child in panel.winfo_children()))

    @unittest.skipUnless(os.environ.get('SO101_COMPARISON_RENDER')=='1','Actual cached comparison render')
    def test_cached_four_model_results_and_render(self):
        panel=ComparisonPanel(self.root,ROOT/'data/model-comparison/index.json');panel.pack(fill='both',expand=True)
        self.root.update()
        self.assertEqual(len(panel.items),31)
        for row in panel.items:
            self.assertEqual(set(row['models']),set(panel.ORDER))
            for key,result in row['models'].items():
                self.assertTrue((panel.folder/result['image']).is_file())
                self.assertEqual(result['runtime']['machine'],'x86_64' if key in ('pc','sam3') else 'aarch64')
                self.assertTrue(all(d['confidence']>=.5 for d in result['detections']))
        out=ROOT/'verification/four-way-viewer-20261018'
        def capture(name):
            self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+1920,y+1043)).save(out/name)
        capture('four-way.png');panel.move(5);capture('four-way-next.png')
        panel.original.set(True);panel.refresh();capture('four-way-original.png')
