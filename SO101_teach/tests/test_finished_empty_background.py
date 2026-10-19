"""Empty-pallet inspection stays inside the projected fixture footprint."""
from copy import deepcopy
import json,unittest
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.episode_inspection import InspectionDecision
from so101_teach.inspection_geometry import project
from so101_teach.shape_inspection import ShapeInspector

class FinishedEmptyBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/finished-empty-background'
        cfg=json.loads((cls.folder/'fixture.json').read_text())
        cls.profile=cfg['profile'];cls.anchor=cfg['anchors'][0]
        cls.anchor['camera_from_local']=np.asarray(cls.anchor['camera_from_local'])
        cls.frame=cv2.imread(str(cls.folder/'inspection.jpg'))

    def inspect(self,frame):
        return ShapeInspector().inspect(frame,[deepcopy(self.anchor)],self.profile)[0]

    def test_actual_empty_sequence_passes_with_the_saved_pose(self):
        check=dict(station='finished',target='pallet',expected='empty',timeout_seconds=10)
        decision=InspectionDecision(check,'A',10);accepted=False
        for i,name in enumerate(('jig-confirmed.jpg','inspection.jpg','last-received.jpg')):
            frame=cv2.imread(str(self.folder/name));row=self.inspect(frame)
            self.assertEqual(row['state'],'empty',row['reason'])
            if not accepted:accepted=decision.observe({'at':10.1+i*.4,'row':row},10.2+i*.4)
        self.assertTrue(accepted)

    def test_color_outside_fixture_does_not_count_as_occupancy(self):
        inspector=ShapeInspector();lo,_,templates=inspector.projected(self.anchor,self.profile,self.frame.shape)
        masks=dict(templates)['finished_A_empty'];outside=(masks['_region'] & ~masks['_empty_region']).astype(np.uint8)
        # Put a coherent colored patch wholly inside the former shared region,
        # beyond this empty fixture. Do not mask or move any actual fixture pixel.
        centers=cv2.erode(outside,np.ones((5,5),np.uint8))
        yy,xx=np.where(centers);self.assertGreater(len(xx),0)
        x,y=int(xx[len(xx)//2]+lo[0]),int(yy[len(yy)//2]+lo[1])
        frame=self.frame.copy();frame[y-2:y+3,x-2:x+3]=(0,0,255)
        self.assertEqual(self.inspect(frame)['state'],'empty')
        # The same amount of color inside the central opening remains evidence
        # of occupancy and must still prevent an empty observation.
        uv=np.rint(project([[0,0,20]],self.anchor,self.profile)[0]).astype(int)
        inside=self.frame.copy();x,y=uv;inside[y-2:y+3,x-2:x+3]=(0,0,255)
        self.assertNotEqual(self.inspect(inside)['state'],'empty')

    def test_empty_evidence_still_requires_actual_edges_and_correct_pose(self):
        for color in ((175,175,175),(30,150,235)):
            frame=self.frame.copy();polygon=project([[-27,-27,25],[27,-27,25],[27,27,25],[-27,27,25]],self.anchor,self.profile)
            cv2.fillConvexPoly(frame,np.rint(polygon).astype(np.int32),color)
            self.assertNotEqual(self.inspect(frame)['state'],'empty')
        moved=deepcopy(self.anchor);moved['camera_from_local'][:3,3]+=moved['camera_from_local'][:3,0]*50
        self.assertNotEqual(ShapeInspector().inspect(self.frame,[moved],self.profile)[0]['state'],'empty')
