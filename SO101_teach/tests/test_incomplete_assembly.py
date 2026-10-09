import json,unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.shape_inspection import ShapeInspector
from so101_teach.episode_inspection import InspectionDecision

class IncompleteAssemblyTests(unittest.TestCase):
    def test_user_reproduced_cap_only_and_tilted_insert_are_rejected_with_and_without_arm(self):
        folder=ROOT/'tests/fixtures/cap-only-reproduction';cfg=json.loads((folder/'fixture.json').read_text())
        for a in cfg['anchors']:a['camera_from_local']=np.asarray(a['camera_from_local'])
        inspector=ShapeInspector()
        for name in ('without-arm.jpg','with-arm.jpg'):
            rows=inspector.inspect(cv2.imread(str(folder/name)),cfg['anchors'],cfg['profile']);by_id={r['id']:r for r in rows}
            middle=next(r for r in rows if r['id'].startswith('linear_'));cap=by_id['finished']
            self.assertEqual(middle['state'],'insert_added');self.assertEqual(middle['quality'],'abnormal');self.assertIn('중단 외형',middle['reason'])
            self.assertEqual(cap['state'],'cap_only');self.assertEqual(cap['quality'],'abnormal');self.assertIn('완성품 아님',cap['reason'])
            for row,station,target,expected in ((middle,'linear','2','insert_added'),(cap,'finished','pallet','cap_added')):
                d=InspectionDecision(dict(station=station,target=target,expected=expected,timeout_seconds=10),'B',10)
                d.observe({'at':10.1,'row':row},10.2)
                with self.assertRaisesRegex(ValueError,'불합격'):d.observe({'at':10.4,'row':row},10.5)

    def test_user_confirmed_b_housing_is_distinguished_from_same_color_cap(self):
        from so101_teach.inspection_geometry import anchors_for
        folder=ROOT/'tests/fixtures/b-housing-seated';cfg=json.loads((folder/'fixture.json').read_text())
        reference=json.loads((ROOT/'inspection/roi_reference.json').read_text());inspector=ShapeInspector()
        decision=InspectionDecision(dict(station='linear',target='2',expected='housing_seated',timeout_seconds=10),'B',10)
        for i in range(3):
            frame=cv2.imread(str(folder/f'frame-{i}.jpg'))
            anchors,_=anchors_for(frame,{},{},{},cfg['profile'],reference,'리니어 조립','전체',cfg['placement'])
            rows=inspector.inspect(frame,anchors,cfg['profile']);housing=rows[1]
            self.assertEqual(housing['state'],'housing_seated');self.assertEqual(housing['quality'],'normal')
            self.assertEqual(rows[0]['state'],'empty')
            self.assertEqual(housing['product'],'B');self.assertTrue(housing['product_certain'])
            passed=decision.observe({'at':10.1+i*.3,'row':housing},10.2+i*.3)
            if i==0:self.assertFalse(passed)
            if i==1:self.assertTrue(passed)
