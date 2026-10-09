"""Real, user-labelled photos plus adverse inputs; never connect hardware."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import unittest
import cv2
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.inspection_geometry import anchors_for
from so101_teach.shape_inspection import ShapeInspector,InspectionWorker

FOLDER=ROOT/'tests/fixtures/part-inspection'

class ShapeInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=json.loads((FOLDER/'fixture.json').read_text());cls.profile=cls.fixture['profile'];cls.placement=cls.fixture['placement']
        cls.reference=json.loads((ROOT/'inspection/roi_reference.json').read_text());cls.inspector=ShapeInspector()
    def anchors(self,frame,placement=None,endpoint='명령 위치'):
        return anchors_for(frame,{},{},{},self.profile,self.reference,'리니어 조립','전체',placement or self.placement,endpoint)[0]
    def image(self,name='insert-added.jpg'):return cv2.imread(str(FOLDER/name))
    def test_slow_processing_does_not_reset_two_frame_confirmation(self):
        from unittest.mock import patch,Mock
        def inspect(*args):
            time.sleep(1.1)
            return [dict(id='target',state='empty',quality='abnormal',product='A',label='부품 없음')]
        with patch('so101_teach.shape_inspection.ShapeInspector',return_value=Mock(inspect=inspect)):
            worker=InspectionWorker()
            try:
                for index in range(2):
                    at=time.monotonic();worker.submit('same',at,self.image(),[],self.profile)
                    until=time.monotonic()+4
                    while (not worker.result or worker.result['at']!=at) and time.monotonic()<until:time.sleep(.02)
                    self.assertEqual(worker.result['at'],at)
                    self.assertEqual(worker.result['rows'][0]['state'],'checking' if index==0 else 'empty')
            finally:worker.close()

    def test_user_confirmed_both_inserts_are_detected_in_independent_frames(self):
        for name in ('insert-added.jpg','insert-added-second.jpg'):
            frame=self.image(name);rows=self.inspector.inspect(frame,self.anchors(frame),self.profile)
            self.assertEqual([r['state'] for r in rows],['insert_added','insert_added'])
            self.assertEqual([r['product'] for r in rows],['A','B'])
            self.assertTrue(all(not r['hidden'] for r in rows))
    def test_fixture_product_requirement_is_independent_of_filter_and_endpoint(self):
        for product in ('전체','A','B'):
            for endpoint in ('전진 목표','후진 목표'):
                anchors,_=anchors_for(self.image(),{},{},{},self.profile,self.reference,'리니어 조립',product,self.placement,endpoint)
                self.assertEqual([a['expected_product'] for a in anchors],['A','B'])
                self.assertTrue(all(any(t.startswith('linear_'+g+'_') for t in a['templates']) for a in anchors for g in ('A','B')))

    def test_wrong_product_and_uncertain_product_cannot_pass_fixture(self):
        from so101_teach.inspection_seating import apply_quality
        for expected,actual in (('A','B'),('B','A')):
            for state in ('housing_seated','insert_added','cap_added'):
                row=dict(state=state,label=actual+' 조립',reason='형상 확인',product=actual,product_certain=True)
                apply_quality(row,dict(part='assembly',expected_product=expected),'inside')
                self.assertEqual(row['quality'],'abnormal');self.assertIn('다른 제품',row['label'])
                row.update(product=expected,product_certain=False)
                apply_quality(row,dict(part='assembly',expected_product=expected),'inside')
                self.assertEqual(row['quality'],'abnormal');self.assertEqual(row['label'],'제품 구분 미확인')
                row.update(product_certain=True)
                apply_quality(row,dict(part='assembly',expected_product=expected),'inside')
                self.assertEqual(row['quality'],'normal')

    def test_cap_and_empty_are_not_called_middle_only_or_complete_pass(self):
        frame=self.image('cap-and-empty.jpg');rows=self.inspector.inspect(frame,self.anchors(frame),self.profile)
        self.assertEqual([r['state'] for r in rows],['cap_added','empty'])
        self.assertIn('housing',rows[0]['hidden']);self.assertIn('미확인',rows[0]['reason'])
        self.assertNotIn('PASS',json.dumps(rows,ensure_ascii=False))
    def test_occlusion_and_uniform_surface_are_unknown(self):
        frame=np.full_like(self.image(),170)
        rows=self.inspector.inspect(frame,self.anchors(frame),self.profile)
        self.assertTrue(all(r['state']=='unknown' for r in rows))
    def test_wrong_endpoint_does_not_reuse_correct_endpoint_classification(self):
        frame=self.image();placement=deepcopy(self.placement);placement['linear_stage']['startup_state']['commanded_mm']=100
        rows=self.inspector.inspect(frame,self.anchors(frame,placement),self.profile)
        self.assertTrue(all(r.get('quality')!='normal' for r in rows))
    def test_last_command_and_manual_target_are_explicit_image_only_references(self):
        frame=self.image();placement=deepcopy(self.placement);placement['linear_stage']['startup_state']={'known':False,'last_commanded_mm':1.5}
        for value,endpoint,source in ((placement,'명령 위치','마지막 명령'),(self.placement,'후진 목표','미리보기')):
            anchors=self.anchors(frame,value,endpoint);self.assertEqual(len(anchors),2)
            self.assertTrue(all(source in a['source'] for a in anchors))
            rows=self.inspector.inspect(frame,anchors,self.profile)
            self.assertTrue(all(r['state']=='insert_added' for r in rows))
    def test_cloud_sources_and_material_surfaces_are_recorded(self):
        metadata=json.loads((ROOT/'inspection/shape_templates.json').read_text())
        for filename,digest in metadata['sources_sha256'].items():self.assertEqual(hashlib.sha256((ROOT.parent/filename).read_bytes()).hexdigest(),digest)
        self.assertEqual(len(metadata['templates']),62)
        self.assertGreater(len(self.inspector.arrays['linear_A_insert_added__insert']),0)
        self.assertEqual(len(self.inspector.arrays['linear_B_cap_added__insert']),0)
    def test_worker_requires_two_new_frames_and_resets_for_new_context(self):
        worker=InspectionWorker();frame=self.image();anchors=self.anchors(frame)
        def submit(key,at):
            worker.submit(key,at,frame,anchors,self.profile)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                r=worker.result
                if r and r['key']==key and r['at']==at:return r
                time.sleep(.01)
            self.fail('Inspector worker timed out')
        try:
            at=time.monotonic();r=submit('retracted',at);self.assertTrue(all(v['state']=='checking' for v in r['rows']))
            r=submit('retracted',at+.3);self.assertTrue(all(v['state']=='insert_added' for v in r['rows']))
            r=submit('changed',at+.6);self.assertTrue(all(v['state']=='checking' for v in r['rows']))
        finally:worker.close();worker.join(2)
        self.assertFalse(worker.is_alive())
    def test_endpoint_geometry_and_active_arm_coordinates(self):
        frame=self.image();back=self.anchors(frame)
        value=deepcopy(self.placement);value['linear_stage']['startup_state']['commanded_mm']=100
        front=self.anchors(frame,value)
        self.assertAlmostEqual(np.linalg.norm(np.array(back[1]['origin'])-back[0]['origin']),80.)
        self.assertGreater(np.linalg.norm(np.array(front[0]['origin'])-back[0]['origin']),90.)
        profile=deepcopy(self.profile);transform=np.eye(4);transform[:3,3]=[236,457,-5];profile['world_from_base']=transform.tolist()
        active,_=anchors_for(frame,{},{},{},profile,self.reference,'리니어 조립','전체',self.placement)
        np.testing.assert_allclose(active[0]['camera_from_local'],back[0]['camera_from_local'])
        np.testing.assert_allclose(active[0]['origin'],np.asarray(back[0]['origin'])-transform[:3,3])
    def test_no_command_shows_both_fixed_endpoints_without_guessing_saved_stroke(self):
        frame=self.image();value=deepcopy(self.placement);value['linear_stage']['startup_state']={'known':False}
        anchors=self.anchors(frame,value);self.assertEqual(len(anchors),4)
        self.assertTrue(all(not a['inspection_allowed'] for a in anchors))
    def test_mismatched_camera_cannot_project_or_classify(self):
        frame=self.image();profile=deepcopy(self.profile);profile['intrinsics']['size']=[1,1]
        anchors,message=anchors_for(frame,{},{},{},profile,self.reference,'리니어 조립','전체',self.placement)
        self.assertFalse(anchors);self.assertIn('보정',message)
    def test_adoption_geometry_is_independent_of_live_detection_and_heading_is_required(self):
        frame=self.image();jid=self.reference['stations']['carrier']['jig_id'];catalog={jid:{'support_height_mm':0}}
        selected={'metric':{'center_xy_mm':[100,200],'yaw_deg':0,'symmetry_deg':360,'mesh_yaw_offset_deg':180},'orientation_verified':True}
        adopted={jid:{'selected':selected,'pose_held':True}}
        anchors,message=anchors_for(frame,{},adopted,catalog,self.profile,self.reference,'운반용 지그','B',{})
        self.assertEqual(len(anchors),3);self.assertIn('채택 위치',message)
        expected=np.array([100,200])-np.array(self.reference['stations']['carrier']['rois'][0]['fixture_center_xyz_mm'][:2])
        np.testing.assert_allclose(anchors[0]['origin'][:2],expected)
        selected['orientation_verified']=False
        anchors,message=anchors_for(frame,{},adopted,catalog,self.profile,self.reference,'운반용 지그','B',{})
        self.assertFalse(anchors);self.assertIn('방향 확인',message)
