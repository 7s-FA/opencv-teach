import json,unittest,time
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.part_labels import PartLabels
from so101_teach.inspection_geometry import all_anchors

class PartLabelTests(unittest.TestCase):
    def test_matches_selected_C_preview_and_covers_no_part(self):
        folder=ROOT/'tests/fixtures/inspection-all';cfg=json.loads((folder/'fixture.json').read_text());frame=cv2.imread(str(folder/'faults.jpg'))
        sig=json.loads((ROOT/'verification/vision-performance/final.json').read_text())['cases'][1]['signature'];rows={r['id']:r for r in sig['rows']}
        anchors,_=all_anchors(frame,sig['detection'],{},cfg['catalog'],cfg['profile'],cfg['reference'],cfg['placement'])
        labels=[dict(id=a['id'],text=a['label'],product=a.get('product'),polygon=rows[a['id']]['roi_display_polygon'],top=rows[a['id']].get('part_top_polygon') or []) for a in anchors]
        layout=PartLabels();start=time.perf_counter();actual=layout.draw(frame.copy(),labels);cold=time.perf_counter()-start
        expected=json.loads((ROOT/'verification/part-label-options/layouts.json').read_text())['C']
        self.assertEqual({r['id']:r['rect'] for r in actual},{r['id']:r['rect'] for r in expected})
        for r in actual:
            x,y,w,h=r['rect'];self.assertFalse(layout.protected[y:y+h,x:x+w].any())
        with patch('so101_teach.part_labels.cv2.integral',side_effect=AssertionError('Unchanged geometry recomputed')):
            fresh=np.full_like(frame,17);start=time.perf_counter();layout.draw(fresh,labels);warm=time.perf_counter()-start
            np.testing.assert_array_equal(fresh[0,0],[17,17,17])
        print('Label layout cold/warm ms:',round(cold*1000,2),round(warm*1000,2))
        layout.draw(frame.copy(),[]);self.assertEqual(layout.rectangles,[])
    def test_fully_covered_image_hides_name_instead_of_covering_part(self):
        frame=np.zeros((100,100,3),np.uint8);layout=PartLabels()
        layout.draw(frame,[dict(id='x',text='A 상단',product='A',polygon=[[0,0],[99,0],[99,99],[0,99]],top=[])])
        self.assertFalse(frame.any());self.assertEqual(layout.rectangles,[])
