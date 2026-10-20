from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest,time,tempfile
import numpy as np
from so101_teach.ai_inspection import combine,ModelInspector
from so101_teach.episode_inspection import InspectionDecision
from so101_teach.model_regions import RegionModel

class AIInspectionTests(unittest.TestCase):
    def setUp(self):
        self.frame=np.full((80,80,3),100,np.uint8);self.roi=np.zeros((40,40),np.uint8);self.roi[5:35,5:35]=255
        self.geometry=Mock();self.geometry.projected.return_value=(np.array([20,20]),np.array([59,59]),[('part',{'roi':self.roi})])
        self.geometry.meta={'part':{'product':'A','state':'present','part':'housing'}}
        self.anchor={'id':'slot','part':'housing','product':'A','inspection_allowed':True}
        self.row={'id':'slot','state':'present','quality':'normal','product':'A','product_certain':True,'template':'part','seating':'inside','reason':'CAD verified','label':'형상 일치','center_error_mm':1.,'center_tolerance_mm':5.,'rotation_error_deg':2.,'rotation_tolerance_deg':12.}
    def item(self,cls=3,box=(30,30,50,50)):
        mask=np.zeros((80,80),bool);x0,y0,x1,y1=box;mask[y0:y1,x0:x1]=True
        return {'class_id':cls,'confidence':.9,'box':list(box),'mask':mask}
    def fuse(self,items,row=None,anchor=None):
        return combine([row or self.row],[anchor or self.anchor],items,self.geometry,self.frame,{},.5,mask_boundary=True)[0]
    def test_agreement_preserves_existing_geometry_and_input(self):
        original=deepcopy(self.row);out=self.fuse([self.item()]);self.assertEqual(out['quality'],'normal');self.assertEqual(out['ai_seating'],'inside')
        for key in ('center_error_mm','center_tolerance_mm','rotation_error_deg','rotation_tolerance_deg'):self.assertEqual(out[key],self.row[key])
        self.assertEqual(self.row,original)
    def test_missing_conflicting_multiple_and_tied_locations_do_not_pass(self):
        for items in ([],[self.item(6)],[self.item(),self.item()]):self.assertNotEqual(self.fuse(items)['quality'],'normal')
        anchors=[self.anchor,{**self.anchor,'id':'other'}];rows=[self.row,{**self.row,'id':'other'}]
        out=combine(rows,anchors,[self.item()],self.geometry,self.frame,{},.5)
        self.assertTrue(all(r['quality']=='waiting' for r in out))
        uncertain=self.fuse([],{**self.row,'quality':'abnormal'})
        d=InspectionDecision({'station':'linear','target':'1','expected':'occupied','timeout_seconds':10},'A',10)
        d.observe({'at':10.1,'row':uncertain},10.2);self.assertFalse(d.last_good)
    def test_mask_is_not_clipped_and_existing_faults_are_not_overridden(self):
        out=self.fuse([self.item(box=(10,30,50,50))]);self.assertEqual(out['quality'],'abnormal');self.assertGreater(out['ai_seating_details']['outside_percent'],0)
        out=self.fuse([self.item()],{**self.row,'quality':'abnormal','reason':'center outside tolerance'})
        self.assertEqual(out['quality'],'abnormal');self.assertIn('center outside',out['reason'])
    def test_empty_needs_current_jig_and_positive_cad_empty_evidence(self):
        anchor={**self.anchor,'part':'finished'};row={**self.row,'state':'empty','quality':'abnormal','seating':'missing'}
        self.assertEqual(self.fuse([self.item(1)],row,anchor)['state'],'empty')
        self.assertEqual(self.fuse([],row,anchor)['state'],'unknown')
        occupied=self.fuse([self.item(1),self.item(3)],row,anchor)
        self.assertNotEqual(occupied['state'],'empty');self.assertNotEqual(occupied['quality'],'normal')
        weak={**row,'state':'unknown','quality':'waiting'};self.assertEqual(self.fuse([self.item(1)],weak,anchor)['state'],'unknown')
    def test_product_ambiguity_only_resolves_with_matching_cad_state(self):
        anchor={**self.anchor,'part':'assembly','expected_product':'A'};row={**self.row,'state':'housing_seated','quality':'waiting','label':'제품 구분 미확인','product_certain':False}
        self.assertEqual(self.fuse([self.item()],row,anchor)['quality'],'normal')
        row['missing_visible_parts']=['housing'];self.assertEqual(self.fuse([self.item()],row,anchor)['quality'],'abnormal')
    def test_ai_label_rechecks_matching_cad_without_moving_the_anchor(self):
        anchor={**self.anchor,'part':'assembly','templates':['part','middle']}
        original={**self.row,'state':'cap_added','quality':'abnormal','rotation_error_deg':19.8,'reason':'angle exceeds 12 degrees'}
        self.geometry.meta['middle']={'product':'A','state':'insert_added'}
        self.geometry.projected.return_value=(np.array([20,20]),np.array([59,59]),[('part',{'roi':self.roi}),('middle',{'roi':self.roi})])
        self.geometry.inspect.return_value=[{**self.row,'template':'middle','state':'insert_added'}]
        out=self.fuse([self.item(9)],original,anchor)
        self.assertEqual(out['state'],'insert_added');self.assertEqual(out['quality'],'abnormal')
        self.assertIn('A 하단+중단',out['label']);self.assertIn('오안착',out['label'])
        self.assertEqual(out['cad_geometric_fault']['rotation_error_deg'],19.8)
        inspected=self.geometry.inspect.call_args.args[1][0]
        self.assertEqual(inspected,{**anchor,'templates':['middle']});self.assertEqual(anchor['templates'],['part','middle'])
        decision=InspectionDecision({'station':'linear','target':'1','expected':'insert_added','timeout_seconds':10},'A',10)
        self.assertFalse(decision.observe({'at':10.1,'row':out},10.2))
        with self.assertRaisesRegex(ValueError,'불합격'):decision.observe({'at':10.4,'row':out},10.5)
    def test_ai_label_cannot_turn_weak_geometry_into_a_pass(self):
        anchor={**self.anchor,'part':'finished','templates':['part','complete']}
        self.geometry.meta['complete']={'product':'A','state':'cap_added'}
        self.geometry.projected.return_value=(np.array([20,20]),np.array([59,59]),[('part',{'roi':self.roi}),('complete',{'roi':self.roi})])
        original={**self.row,'state':'housing_seated'}
        self.geometry.inspect.return_value=[{**self.row,'template':'complete','state':'unknown','quality':'waiting','reason':'weak CAD'}]
        out=self.fuse([self.item(11)],original,anchor)
        self.assertEqual(out['state'],'cap_added');self.assertEqual(out['quality'],'waiting');self.assertIn('A 완제품',out['label'])
        self.geometry.inspect.return_value=[{**self.row,'template':'complete','state':'cap_added','missing_visible_parts':['insert']}]
        self.assertEqual(self.fuse([self.item(11)],original,anchor)['quality'],'abnormal')
        self.geometry.inspect.return_value=[{**self.row,'template':'complete','state':'cap_added'}]
        self.assertEqual(self.fuse([self.item(11)],original,anchor)['quality'],'normal')
    def test_dark_background_spur_is_excluded_without_clipping_real_displacement(self):
        # The body is inside; a model spur reaches beyond the allowed region.
        self.frame[:]=0;self.frame[30:50,30:50]=(160,90,140)
        item=self.item();item['mask'][35:45,15:31]=True
        original=item['mask'].copy();out=self.fuse([item])
        self.assertEqual(out['ai_raw_seating'],'boundary')
        self.assertEqual(out['ai_seating'],'inside');self.assertEqual(out['quality'],'normal')
        self.assertGreater(out['ai_mask_dark_pixels'],0)
        np.testing.assert_array_equal(item['mask'],original)
        # The same protrusion with actual image evidence must still fail, even
        # at the first visible value above the unchanged V > 30 floor.
        self.frame[35:45,15:31]=(31,0,0)
        displaced=self.fuse([item]);self.assertEqual(displaced['ai_seating'],'boundary')
        self.assertEqual(displaced['quality'],'abnormal')
    def test_dark_mask_without_visible_body_is_inconclusive(self):
        self.frame[:]=30
        out=self.fuse([self.item()])
        self.assertEqual(out['state'],'unknown');self.assertEqual(out['quality'],'waiting')
        self.assertEqual(out['ai_seating'],'missing')
    def test_model_lifecycle_error_and_legacy_profile(self):
        self.geometry.inspect.return_value=[self.row];provider=Mock();provider.predict.return_value=[self.item()];inspector=ModelInspector(self.geometry,provider)
        inspector.inspect(self.frame,[self.anchor],{});provider.predict.assert_not_called()
        settings={'jig_detection':{'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}}
        inspector.inspect(self.frame,[self.anchor],settings);provider.predict.assert_called_once()
        provider.predict.side_effect=RuntimeError('model unavailable')
        with self.assertRaisesRegex(RuntimeError,'unavailable'):inspector.inspect(self.frame,[self.anchor],settings)
        inspector.close();self.assertEqual(provider.close.call_count,2)
        self.assertEqual(RegionModel().class_ids,(0,1));self.assertEqual(len(RegionModel(class_ids=tuple(range(13))).class_ids),13)
    def test_production_uses_labels_and_cad_without_mask_boundary_veto(self):
        self.geometry.inspect.return_value=[self.row]
        provider=Mock();provider.predict.return_value=[self.item(box=(10,30,50,50))]
        inspector=ModelInspector(self.geometry,provider)
        profile={'part_inspection':{'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}}
        with patch('so101_teach.ai_inspection.containment',side_effect=AssertionError('No mask boundary gate in production')):
            out=inspector.inspect(self.frame,[self.anchor],profile)[0]
        self.assertEqual(out['quality'],'normal');self.assertEqual(out['ai_label'],'A 하단')
        self.assertFalse(out['mask_boundary_check']);self.assertNotIn('ai_seating',out)
        for fault in (
            {'seating':'boundary','quality':'abnormal'},
            {'rotation_error_deg':20.,'quality':'abnormal'},
            {'center_error_mm':8.,'quality':'abnormal'},
        ):
            self.geometry.inspect.return_value=[{**self.row,**fault}]
            out=inspector.inspect(self.frame,[self.anchor],profile)[0]
            self.assertEqual(out['quality'],'abnormal')
    def test_explicit_inspection_selection_is_independent_of_jig_detection(self):
        from so101_teach.ai_inspection import inspection_settings,inspector_for
        ai={'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}
        profile={'jig_detection':ai,'part_inspection':{'method':'opencv'}}
        self.assertEqual(inspection_settings(profile)['method'],'opencv')
        with patch('so101_teach.shape_inspection.ShapeInspector',return_value=self.geometry):
            self.assertIs(inspector_for(profile),self.geometry)
        self.geometry.inspect.return_value=[self.row];provider=Mock();provider.predict.return_value=[self.item()]
        inspector=ModelInspector(self.geometry,provider)
        self.assertEqual(inspector.inspect(self.frame,[self.anchor],profile),[self.row]);provider.predict.assert_not_called()
        profile={'jig_detection':{'method':'opencv'},'part_inspection':ai}
        self.assertEqual(inspector.inspect(self.frame,[self.anchor],profile)[0]['inspection_method'],'ai_and_cad')
        provider.predict.assert_called_once_with(self.frame,ai)
        for invalid in (None,{'method':'other'},dict(ai,model_identity=None)):
            with self.assertRaises(ValueError):inspection_settings({'part_inspection':invalid})
    def test_ai_observation_does_not_bypass_two_fresh_observations(self):
        row=self.fuse([self.item()]);check={'station':'carrier','target':'housing','expected':'present','timeout_seconds':10}
        d=InspectionDecision(check,'A',10)
        self.assertFalse(d.observe({'at':10.1,'row':row},10.2));self.assertFalse(d.observe({'at':10.1,'row':row},10.3))
        self.assertTrue(d.observe({'at':10.4,'row':row},10.5))
    def test_grouped_runtime_uses_one_ai_call_and_closes_worker(self):
        from so101_teach.episode_inspection_runtime import FrameInspections
        anchor={**self.anchor,'part':'finished'};row={**self.row,'state':'cap_added'}
        self.geometry.inspect.return_value=[row];provider=Mock();provider.predict.return_value=[self.item(11)]
        profile={'jig_detection':{'method':'model_roi','model':'n','confidence':.5,'model_identity':'a'*64}}
        steps=[{'id':key,'inspection_product':'A','inspection':{'station':'finished','target':'pallet','expected':'cap_added','timeout_seconds':10}} for key in ('one','two')]
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.shape_inspection.ShapeInspector',return_value=self.geometry),patch('so101_teach.ai_inspection.RegionModel',return_value=provider),patch('so101_teach.inspection_geometry.anchors_for',return_value=([anchor],'fixed')):
            worker=FrameInspections(folder,profile,SimpleNamespace(items={}),{},steps,fixed_jigs={})
            try:
                worker.submit(self.frame,time.monotonic());until=time.monotonic()+2
                while worker.result is None and time.monotonic()<until:time.sleep(.01)
                self.assertIsNotNone(worker.result);self.assertNotIn('error',worker.result)
                self.assertEqual(set(worker.result['results']),{'one','two'})
                self.assertTrue(all(v['row']['inspection_method']=='ai_and_cad' for v in worker.result['results'].values()))
                provider.predict.assert_called_once()
            finally:worker.close();worker.join(2)
            provider.close.assert_called_once()
