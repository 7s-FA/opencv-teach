"""Occupied pallet recovery is an inspection anchor, never a motion measurement."""
import json,time,unittest
from copy import deepcopy
import cv2
from so101_teach.domain import ROOT
from so101_teach.occupied_pallet import locate
from so101_teach.jig_consensus import StableCandidate
from so101_teach.shape_inspection import InspectionWorker

class OccupiedPalletRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/inspection-all';cls.cfg=json.loads((cls.folder/'fixture.json').read_text());cls.config=cls.cfg['catalog']['pallet']
    def test_nearby_blue_object_is_not_a_pallet(self):
        folder=ROOT/'tests/fixtures/vision-precision';cfg=json.loads((folder/'fixture.json').read_text())
        self.assertIsNone(locate(cv2.imread(str(folder/'frame3.jpg')),cfg['jigs']['pallet'],cfg['profile']))
    def test_occupied_anchor_cannot_enter_pose_consensus(self):
        result=locate(cv2.imread(str(self.folder/'complete.jpg')),self.config,self.cfg['profile'])
        self.assertTrue(result['inspection_only']);self.assertGreater(result['selected']['white_rim_fraction'],.70)
        # Even a caller incorrectly adding ordinary-pose metadata cannot adopt it.
        candidate=deepcopy(result['selected']);candidate.update(outer_shape={'valid':True},area_px=10000,score=1.)
        unsafe={**result,'status':'shape_match','selected':candidate,'candidates':[candidate]}
        self.assertIsNone(StableCandidate.candidate(unsafe))
    def inspect(self,name):
        frame=cv2.imread(str(self.folder/name));worker=InspectionWorker()
        try:
            for i in range(2):
                at=time.monotonic();worker.submit('pallet-test',at,frame,[],self.cfg['profile'],recovery={'catalog':self.cfg['catalog'],'reference':self.cfg['reference'],'placement':self.cfg['placement'],'reference_directory':'/nonexistent'})
                until=time.monotonic()+10
                while time.monotonic()<until and (worker.result is None or worker.result['at']!=at):time.sleep(.01)
                self.assertIsNotNone(worker.result);self.assertEqual(worker.result['at'],at);self.assertIsNone(worker.result['error'])
            row=next(r for r in worker.result['rows'] if r['id']=='finished')
            self.assertIn('부품 검사 전용',next(a for a in worker.result['anchors'] if a['id']=='finished')['source'])
            return row
        finally:worker.close()
    def test_loaded_complete_classification_works_without_empty_reference(self):
        row=self.inspect('complete.jpg');self.assertEqual(row['state'],'cap_added');self.assertEqual(row['quality'],'normal')
    def test_loaded_incomplete_is_not_accepted_as_complete(self):
        row=self.inspect('faults.jpg');self.assertEqual(row['state'],'insert_added');self.assertEqual(row['quality'],'abnormal')
