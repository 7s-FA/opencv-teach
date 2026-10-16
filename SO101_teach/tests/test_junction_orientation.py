"""Junction fallback stays image-based and never invents a full heading."""
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach import junction_orientation as junction
from so101_teach.vision import detect,stl_profile,annotate
from so101_teach.height_reference import support_plane_profile
from so101_teach.jig_consensus import StableCandidate
from so101_teach.inspection_geometry import all_anchors

class JunctionOrientationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/inspection-all';cls.cfg=json.loads((cls.folder/'fixture.json').read_text())
        cls.key='b5ebdb54807c441c8267c15d77713b6a';cls.jig=cls.cfg['catalog'][cls.key]
        cls.profile=support_plane_profile(cls.cfg['profile'],cls.jig)
        cls.mesh={**stl_profile(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'shape':'rectangle','method':'grid'}
    def detect(self,name='faults.jpg'):
        return detect(cv2.imread(str(self.folder/name)),self.mesh,self.jig['roi'],self.profile)
    def test_circle_priority_never_invokes_junction(self):
        with patch.object(junction,'evaluate',side_effect=AssertionError('unexpected fallback')):
            result=self.detect('complete.jpg')
        self.assertEqual(result['selected']['orientation_method'],'circular_opening')
    def test_fallback_reaches_adoption_inspection_and_overlay_without_circle_metadata(self):
        result=self.detect();candidate=result['selected'];self.assertEqual(candidate['orientation_method'],'central_junction')
        self.assertNotIn('orientation_hole_outline_px',candidate)
        self.assertGreaterEqual(candidate['orientation_junction_margin'],.08)
        stable=StableCandidate(3)
        for at in (0,.8,1.6,2.4):stable.update(result,at,profile=self.profile,mesh=self.mesh)
        adopted=stable.finish(3)['selected'];self.assertEqual(adopted['orientation_method'],'central_junction');self.assertEqual(adopted['symmetry_deg'],360)
        frame=cv2.imread(str(self.folder/'faults.jpg'));drawn=annotate(frame,{**result,'live_view':True});self.assertEqual(drawn.shape,frame.shape)
        anchors,_=all_anchors(frame,{self.key:result},{},self.cfg['catalog'],self.cfg['profile'],self.cfg['reference'],self.cfg['placement'])
        self.assertEqual(sum(r['station']=='운반용 지그' for r in anchors),6)
    def test_missing_reference_keeps_direction_unconfirmed(self):
        junction.reference_evidence.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as directory,patch.object(junction,'REFERENCE',Path(directory)/'missing.png'):
                result=self.detect()
            self.assertIsNone(result['selected']);self.assertEqual(result['status'],'orientation_unconfirmed')
        finally:junction.reference_evidence.cache_clear()
    def test_ambiguous_junction_never_becomes_a_selected_pose(self):
        with patch.object(junction,'evaluate',return_value={'verified':False,'scores':[{'score':.70},{'score':.68}],'reason':'점수 차이 부족'}):result=self.detect()
        self.assertIsNone(result['selected']);self.assertEqual(result['status'],'orientation_unconfirmed')
    def test_blank_patch_does_not_match_reference(self):
        for value in (0,128,255):self.assertLess(junction.compare(np.full((241,281,3),value,np.uint8))['score'],junction.MIN_SCORE)
    def test_roi_cannot_be_bypassed_by_junction(self):
        candidate={'metric':{'center_xy_mm':[196.79,-130.14],'yaw_deg':29.57},'boundary_scale':1.0198}
        frame=cv2.imread(str(self.folder/'faults.jpg'))
        result=junction.evaluate(frame,self.mesh,np.zeros(frame.shape[:2],np.uint8),self.profile,candidate)
        self.assertFalse(result['verified']);self.assertIn('ROI',result['reason'])
