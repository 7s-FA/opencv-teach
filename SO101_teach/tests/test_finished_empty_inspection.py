import json,unittest
from copy import deepcopy
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.shape_inspection import ShapeInspector
from so101_teach.episode_inspection import InspectionDecision,GroupInspectionDecision
from so101_teach.inspection_geometry import project

class FinishedEmptyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=ROOT/'tests/fixtures/finished-empty-arm3'
        cls.fixture=json.loads((folder/'fixture.json').read_text());cls.frame=cv2.imread(str(folder/'camera.jpg'))
        cls.anchor=cls.fixture['anchors'][0];cls.anchor['camera_from_local']=np.asarray(cls.anchor['camera_from_local'])
        cls.profile=cls.fixture['profile']
    def inspect(self,frame):return ShapeInspector().inspect(frame,[deepcopy(self.anchor)],self.profile)[0]
    def test_real_empty_white_pallet_passes_empty_check(self):
        row=self.inspect(self.frame)
        self.assertEqual(row['state'],'empty',row)
        check=dict(station='finished',target='pallet',expected='empty',timeout_seconds=10)
        d=InspectionDecision(check,'B',10);self.assertFalse(d.observe({'at':10.1,'row':row},10.2))
        self.assertTrue(d.observe({'at':10.4,'row':row},10.5))
    def test_uniform_cover_and_colored_occupancy_are_not_empty(self):
        for color in ((175,175,175),(30,150,235)):
            frame=self.frame.copy()
            polygon=project([[-27,-27,25],[27,-27,25],[27,27,25],[-27,27,25]],self.anchor,self.profile)
            cv2.fillConvexPoly(frame,np.rint(polygon).astype(np.int32),color)
            self.assertNotEqual(self.inspect(frame)['state'],'empty')
    def test_lost_or_incorrect_pallet_pose_cannot_supply_empty_evidence(self):
        blank=np.full_like(self.frame,175);self.assertNotEqual(self.inspect(blank)['state'],'empty')
        anchor=deepcopy(self.anchor);anchor['camera_from_local'][:3,3]+=anchor['camera_from_local'][:3,0]*50
        row=ShapeInspector().inspect(self.frame,[anchor],self.profile)[0]
        self.assertNotEqual(row['state'],'empty')
    def test_unknown_timeout_keeps_scores_and_target_coordinates(self):
        frame=self.frame.copy()
        polygon=project([[-16,-16,20],[16,-16,20],[16,16,20],[-16,16,20]],self.anchor,self.profile)
        cv2.fillConvexPoly(frame,np.rint(polygon).astype(np.int32),(175,175,175))
        row=self.inspect(frame);self.assertEqual(row['state'],'unknown')
        step=dict(id='empty',name='시작 전 완성품 팔레트',inspection_product='B',inspection=dict(station='finished',target='pallet',expected='empty',timeout_seconds=10))
        group=GroupInspectionDecision([step],10)
        group.observe({'results':{'empty':{'at':10.1,'row':row}}},10.2)
        with self.assertRaises(ValueError) as caught:group.observe(None,20)
        for text in ('일치','외곽','상태차','검사 중심','X +124.3','Y +163.6'):self.assertIn(text,str(caught.exception))
