"""Actual empty-A failure frames and target-only checks with neighbour context."""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock,patch
import hashlib,json,tempfile,time,unittest
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.inspection_geometry import inspection_context
from so101_teach.shape_inspection import ShapeInspector
from so101_teach.episode_inspection import InspectionDecision
from so101_teach.episode_inspection_runtime import FrameInspection,FrameInspections
from so101_teach.ai_inspection import combine

FOLDER=ROOT/'tests/fixtures/neighbor-inspection'

class NeighbourInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=json.loads((FOLDER/'fixture.json').read_text())
        cls.profile=cls.fixture['profile'];cls.placement=cls.fixture['placement'];cls.step=cls.fixture['step']
        cls.reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
    def geometry(self,frame):
        located,context=inspection_context(frame,{},{},{},self.profile,self.reference,self.placement)
        return [a for a in located['linear'][0] if a['expected_product']=='A'],context
    def test_saved_failure_is_reproduced_without_context_and_fixed_with_it(self):
        frame=cv2.imread(str(FOLDER/'inspection.jpg'));targets,context=self.geometry(frame);inspector=ShapeInspector()
        old=inspector.inspect(frame,targets,self.profile)[0];new=inspector.inspect(frame,targets,self.profile,context_anchors=context)[0]
        self.assertEqual((old['state'],old['quality'],old['body_pixels']),('empty','outside',4851))
        self.assertEqual(old['observed_polygon'],self.fixture['original_row']['observed_polygon'])
        self.assertEqual(new['roi_polygon'],old['roi_polygon']);self.assertEqual(new['state'],'empty')
        self.assertEqual(new['seating'],'missing');self.assertEqual(new['body_pixels'],0)
        self.assertEqual(new['excluded_neighbor_components'][0]['fixture_id'],'linear_1.5_1')
    def test_two_independently_captured_error_frames_pass_the_same_empty_criteria(self):
        frames=self.fixture['frames'];self.assertNotEqual(hashlib.sha256((FOLDER/frames[0]['file']).read_bytes()).digest(),hashlib.sha256((FOLDER/frames[1]['file']).read_bytes()).digest())
        started=frames[0]['captured_at']-.1;decision=InspectionDecision(self.step['inspection'],'A',started);inspector=ShapeInspector();passes=[]
        for item in frames:
            frame=cv2.imread(str(FOLDER/item['file']));targets,context=self.geometry(frame)
            row=inspector.inspect(frame,targets,self.profile,context_anchors=context)[0]
            passes.append(decision.observe({'at':item['captured_at'],'row':row},item['captured_at']+.1))
        self.assertEqual(passes,[False,True])
    def test_nearer_neighbour_without_containment_cannot_hide_outside_part(self):
        frame=cv2.imread(str(FOLDER/'inspection.jpg'));targets,context=self.geometry(frame);inspector=ShapeInspector()
        projected=inspector.projected
        def outside_neighbour(anchor,*args):
            lo,hi,templates=projected(anchor,*args)
            if anchor['id']=='linear_1.5_1':
                templates=[(name,{**m,'roi_polygon':[[0,0],[1,0],[1,1],[0,1]]}) for name,m in templates]
            return lo,hi,templates
        with patch.object(inspector,'projected',side_effect=outside_neighbour):
            row=inspector.inspect(frame,targets,self.profile,context_anchors=context)[0]
        self.assertEqual(row['quality'],'outside');self.assertEqual(row['body_pixels'],4851)
    def worker(self,group=False):
        profile=deepcopy(self.profile);profile['part_inspection']={'method':'opencv'}
        catalog=SimpleNamespace(items={},mesh=Mock(side_effect=AssertionError('No new jig detection')))
        inspector=ShapeInspector();calls=[]
        class Borrowed:
            stop=None
            def inspect(self,frame,anchors,profile,**kwargs):
                calls.append((len(anchors),[a['id'] for a in kwargs['context_anchors']]))
                return inspector.inspect(frame,anchors,profile,**kwargs)
            def close(self):pass
        prepared=SimpleNamespace(borrow=Borrowed,visual=None)
        with tempfile.TemporaryDirectory() as folder:
            if group:
                steps=[{**self.step,'inspection_product':'A'},
                    {'id':'already-checked-B','name':'B','inspection':{**self.step['inspection'],'target':'2'},'inspection_product':'B'}]
                worker=FrameInspections(folder,profile,catalog,self.placement,steps,fixed_jigs={},prepared_inspector=prepared)
                worker.retain([self.step['id']])
            else:worker=FrameInspection(folder,profile,catalog,self.placement,self.step['inspection'],'A',fixed_jigs={},prepared_inspector=prepared)
            try:
                worker.submit(cv2.imread(str(FOLDER/'inspection.jpg')),time.monotonic());deadline=time.monotonic()+3
                while worker.result is None and time.monotonic()<deadline:time.sleep(.01)
                self.assertIsNotNone(worker.result);self.assertNotIn('error',worker.result)
                result=worker.result['results'][self.step['id']] if group else worker.result
                self.assertEqual(result['row']['seating'],'missing');self.assertEqual(calls,[(1,['linear_1.5_0','linear_1.5_1'])])
                if group:self.assertEqual(set(worker.result['results']),{self.step['id']})
            finally:worker.close()
    def test_mid_step_checks_classify_only_target_but_keep_both_fixture_positions(self):self.worker()
    def test_startup_check_keeps_completed_neighbour_when_only_A_remains_pending(self):self.worker(group=True)
    def test_moving_unknown_linear_stage_cannot_be_a_static_neighbour(self):
        frame=cv2.imread(str(FOLDER/'inspection.jpg'))
        for state in ({'known':False},{'known':True,'moving':True},{'known':True,'pending':True}):
            placement=deepcopy(self.placement);placement['linear_stage']['startup_state']=state
            located,context=inspection_context(frame,{},{},{},self.profile,self.reference,placement)
            self.assertEqual(located['linear'][0],[]);self.assertFalse(context)

class NeighbourAIInspectionTests(unittest.TestCase):
    def test_object_belongs_to_unchecked_neighbour_instead_of_empty_target(self):
        frame=np.full((80,80,3),100,np.uint8)
        def mask(x0,x1):
            m=np.zeros((80,80),np.uint8);m[10:50,x0:x1]=255;return m
        a={'id':'A','part':'assembly','inspection_allowed':True};b={**a,'id':'B'}
        geometry=Mock();geometry.projected.side_effect=lambda anchor,*args:(np.array([0,0]),np.array([79,79]),[('part',{'roi':mask(5,35) if anchor['id']=='A' else mask(30,70)})])
        row={'id':'A','state':'empty','quality':'abnormal','seating':'missing','reason':'빈 지그 형상'}
        objects=[{'class_id':2,'confidence':.95,'box':[5,10,35,50],'mask':mask(5,35)>0},
                 {'class_id':8,'confidence':.95,'box':[32,10,65,50],'mask':mask(32,65)>0}]
        result=combine([row],[a],objects,geometry,frame,{},.5,context_anchors=[a,b])[0]
        self.assertEqual(result['state'],'empty');self.assertEqual(result['ai_observations'],[])
        geometry.inspect.assert_not_called()
    def test_equal_association_with_neighbour_is_inconclusive(self):
        frame=np.full((40,40,3),100,np.uint8);roi=np.ones((40,40),np.uint8)*255
        a={'id':'A','part':'assembly','inspection_allowed':True};b={**a,'id':'B'}
        geometry=Mock();geometry.projected.return_value=(np.array([0,0]),np.array([39,39]),[('part',{'roi':roi})])
        row={'id':'A','state':'empty','quality':'abnormal','seating':'missing','reason':'빈 지그 형상'}
        item={'class_id':3,'confidence':.9,'box':[0,0,39,39],'mask':roi>0}
        result=combine([row],[a],[item],geometry,frame,{},.5,context_anchors=[a,b])[0]
        self.assertEqual(result['state'],'unknown');self.assertEqual(result['quality'],'waiting')
