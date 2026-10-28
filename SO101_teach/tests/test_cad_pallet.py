"""CAD-only pallet / OpenCV carrier contracts using only the current workstation photographs."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import hashlib,json,unittest
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.mask_pose import detect_mask_seeded
from so101_teach.vision_service import MultiDetector

class CadPalletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/cad-pallet-current';cls.data=json.loads((cls.folder/'fixture.json').read_text());cls.settings=cls.data['settings']
    def inputs(self,name):
        case=deepcopy(self.data['cases'][name]);path=self.folder/(name+'.png');self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),case['image_sha256']);frame=cv2.imread(str(path));a=np.load(self.folder/(name+'.npz'));rows=[{'class_id':int(k),'confidence':float(s),'box':b.tolist(),'mask':m} for k,s,b,m in zip(a['classes'],a['scores'],a['boxes'],a['masks'])];return case,frame,rows
    def call(self,name,*,frame=None,rows=None,roi=None,mesh=None):
        c,im,ds=self.inputs(name);o=c['jigs']['pallet'];return detect_mask_seeded(im if frame is None else frame,o['mesh'] if mesh is None else mesh,o['roi'] if roi is None else roi,o['profile'],ds if rows is None else rows,self.settings)
    def test_current_empty_and_loaded_pallets_use_only_cad_pose(self):
        for name in ['C001','C256','C062','C094','C100']:
            with self.subTest(name=name):
                raw=self.call(name);chosen=raw['selected'];self.assertIsNotNone(chosen,raw.get('cad_pallet_reason'));self.assertTrue(chosen['cad_pose_validated']);self.assertFalse(raw['verified_for_motion']);self.assertEqual(chosen['metric']['dimension_source'],'cad_silhouette_fit');self.assertLessEqual(raw['cad_fit']['rms_px'],2.);self.assertGreaterEqual(raw['cad_fit']['observed_sides'],2);self.assertTrue(raw['cad_fit']['pose_constrained'])
                if not raw['cad_fit']['partial_silhouette']:
                    p=self.data['cases'][name]['cad_reference'];np.testing.assert_allclose(chosen['metric']['center_xy_mm'],p['center_xy_mm'],atol=1e-3);self.assertAlmostEqual(chosen['metric']['yaw_deg'],p['yaw_deg'],delta=.001)
    def test_carrier_pose_and_evidence_survive_search_roi_improvements(self):
        c,frame,rows=self.inputs('C256');key='b5ebdb54807c441c8267c15d77713b6a';o=c['jigs'][key]
        raw=detect_mask_seeded(frame,o['mesh'],o['roi'],o['profile'],rows,self.settings)
        previous=self.data['carrier_reference']['selected'];current=raw['selected']
        # Search-region metadata and grid crossing choices legitimately changed
        # with the smaller ROI; physical pose and required evidence must not.
        self.assertEqual(current['metric'],previous['metric'])
        np.testing.assert_allclose(current['quad'],previous['quad'],atol=1e-8)
        for flag in ('shape_match','observed_boundary_refined','grid_verified','orientation_verified'):
            self.assertTrue(current[flag])
        self.assertTrue(current['outer_shape']['valid'])
        self.assertFalse(raw['verified_for_motion'])
    def test_bad_dimensions_reject_and_nonparallel_sides_constrain_pose(self):
        _,_,rows=self.inputs('C001');d=next(x for x in rows if x['class_id']==1)
        d['mask']=cv2.dilate(d['mask'].astype(np.uint8),np.ones((7,7),np.uint8))>0
        raw=self.call('C001',rows=rows);self.assertIsNone(raw['selected']);self.assertEqual(raw['candidates'],[]);self.assertEqual(raw['cad_pallet_reason'],'cad_fit');self.assertTrue(raw['cad_fit']['converged']);self.assertGreater(raw['cad_fit']['rms_px'],3.)
        raw=self.call('C060');self.assertIsNotNone(raw['selected']);self.assertTrue(raw['cad_fit']['pose_constrained']);self.assertEqual(raw['cad_fit']['observed_sides'],2)
    def test_three_pixel_tolerance_accepts_marginal_fit_but_not_larger_error(self):
        from so101_teach.cad_pallet import MAX_CAD_ERROR_PX
        self.assertEqual(MAX_CAD_ERROR_PX,3.)
        for size in [3,5]:
            _,_,rows=self.inputs('C001');d=next(x for x in rows if x['class_id']==1);d['mask']=cv2.dilate(d['mask'].astype(np.uint8),np.ones((size,size),np.uint8))>0
            raw=self.call('C001',rows=rows);self.assertIsNotNone(raw['selected']);self.assertGreater(raw['cad_fit']['rms_px'],2.);self.assertLessEqual(raw['cad_fit']['rms_px'],3.);self.assertEqual(raw['cad_fit']['rms_limit_px'],3.)
    def test_real_loaded_b_ignores_unobserved_hull_chord(self):
        raw=self.call('B051');self.assertIsNotNone(raw['selected']);self.assertGreater(self.data['cases']['B051']['previous_rms_px'],2.)
        self.assertTrue(raw['cad_fit']['partial_silhouette']);self.assertEqual(raw['cad_fit']['observed_sides'],3);self.assertLess(raw['cad_fit']['rms_px'],1.)
        _,frame,_=self.inputs('B051');self.assertEqual(raw,self.call('B051',frame=np.zeros_like(frame)))
    def test_rotated_loaded_a_uses_two_independent_cad_sides(self):
        raw=self.call('A000');self.assertIsNotNone(raw['selected']);self.assertTrue(raw['cad_fit']['partial_silhouette']);self.assertEqual(raw['cad_fit']['observed_sides'],2);self.assertTrue(raw['cad_fit']['pose_constrained']);self.assertGreater(raw['cad_fit']['side_cross'],.9);self.assertLess(raw['cad_fit']['rms_px'],2.)
    def test_occluded_synthetic_cad_recovers_known_pose(self):
        from so101_teach.cad_pallet import model_hull,fit_pose
        from so101_teach.lens_geometry import DetectionFrame
        c,frame,_=self.inputs('B051');o=c['jigs']['pallet'];mesh=o['mesh'];profile=DetectionFrame(frame,o['profile']).profile
        T=np.linalg.inv(profile['extrinsics']['base_from_camera']);K=np.asarray(profile['intrinsics']['K'])
        for yaw,hidden in [(15.,12),(39.,12),(65.,12),(65.,-15)]:
            theta=np.deg2rad(yaw);rot=np.array([[np.cos(theta),np.sin(theta)],[-np.sin(theta),np.cos(theta)]])
            def project(points):
                world=points.copy();world[:,:2]=points[:,:2]@rot+[375,277];camera=world@T[:3,:3].T+T[:3,3];p=camera@K.T;return p[:,:2]/p[:,2:]
            points=model_hull(mesh['sha256']).copy();points[:,:2]-=np.array(mesh['low_mm'][:2])+np.array(mesh['size_mm'][:2])/2;points[:,2]+=profile['table_z_mm']-mesh['low_mm'][2]
            mask=np.zeros(frame.shape[:2],np.uint8);cv2.fillConvexPoly(mask,np.rint(cv2.convexHull(project(points).astype(np.float32))).astype(np.int32),255)
            right=20 if hidden==12 else 25
            cut=np.c_[np.array([[-45,hidden],[right,hidden],[right,60],[-45,60]]),np.full(4,profile['table_z_mm']+mesh['rim_z_mm']-mesh['low_mm'][2])];cv2.fillConvexPoly(mask,np.rint(project(cut)).astype(np.int32),0)
            fit=fit_pose(mask,mesh,profile);self.assertTrue(fit['partial_silhouette']);self.assertGreaterEqual(fit['observed_sides'],2);self.assertTrue(fit['pose_constrained']);self.assertLess(fit['rms_px'],2.)
            # Rasterizing this real camera geometry quantizes the silhouette.
            self.assertLess(np.linalg.norm(np.array(fit['pose']['center_xy_mm'])-[375,277]),.6);self.assertLess(abs((fit['pose']['yaw_deg']-yaw+45)%90-45),.6)
    def test_parallel_boundaries_do_not_constrain_translation(self):
        from so101_teach.cad_pallet import boundary_support
        quad=np.array([[0.,0.],[100.,0.],[100.,100.],[0.,100.]])
        t=np.linspace(.1,.9,24)
        top=np.c_[100*t,np.zeros_like(t)];bottom=np.c_[100*t,np.full_like(t,100)];right=np.c_[np.full_like(t,100),100*t]
        parallel=boundary_support(np.r_[top,bottom],quad);self.assertEqual(parallel['observed_sides'],2);self.assertFalse(parallel['pose_constrained'])
        adjacent=boundary_support(np.r_[top,right],quad);self.assertEqual(adjacent['observed_sides'],2);self.assertTrue(adjacent['pose_constrained'])
    def test_roi_and_fixed_asset_size_still_reject(self):
        self.assertIsNone(self.call('C101')['selected']);self.assertIsNone(self.call('C001',roi=[0,0,.1,.1])['selected']);mesh=deepcopy(self.data['cases']['C001']['jigs']['pallet']['mesh']);mesh['size_mm'][0]=100;self.assertIsNone(self.call('C001',mesh=mesh)['selected'])
    def test_roi_rejection_reports_coverage_to_inspection_view(self):
        from so101_teach.inspection_geometry import anchors_for
        result=self.call('C001',roi=[0,0,.1,.1])
        self.assertEqual(result['cad_pallet_reason'],'roi')
        self.assertLess(result['roi_visible_fraction'],.98);self.assertEqual(result['roi_required_fraction'],.98)
        self.assertIn('필요 98%',result['error'])
        c,frame,_=self.inputs('C001');profile=c['jigs']['pallet']['profile']
        reference={'stations':{'finished_pallet':{'jig_id':'pallet'}}}
        anchors,message=anchors_for(frame,{'pallet':result},{},{},profile,reference,'완성품 팔레트','전체',{})
        self.assertFalse(anchors);self.assertIn(result['error'],message)
    def test_rgb_edges_and_colors_do_not_change_pallet_result(self):
        # Fixed current masks deliberately isolate pose calculation from inference.
        # A live blank image still needs a fresh AI detection (tested separately).
        for name in ['C001','C256','C062','C094','C100']:
            with self.subTest(name=name):
                _,frame,_=self.inputs(name);expected=self.call(name)
                for altered in [np.zeros_like(frame),255-frame]:
                    self.assertEqual(self.call(name,frame=altered),expected)
                self.assertNotIn('pallet_validation',expected)
                self.assertEqual(expected['pallet_pose_method'],'cad_silhouette_fit')
    def test_invalid_masks_do_not_produce_a_cad_pose(self):
        _,frame,rows=self.inputs('C001')
        for mask in [np.zeros(frame.shape[:2],bool),np.ones(frame.shape[:2],bool)]:
            ds=deepcopy(rows);next(d for d in ds if d['class_id']==1)['mask']=mask
            raw=self.call('C001',rows=ds);self.assertIsNone(raw['selected']);self.assertEqual(raw['candidates'],[])
    def test_each_new_cad_observation_uses_a_current_mask(self):
        c,frame,rows=self.inputs('C001');o=c['jigs']['pallet'];catalog=SimpleNamespace(items={'pallet':{'roi':o['roi'],'support_height_mm':0}},revision=0,mesh=lambda key:deepcopy(o['mesh']));d=MultiDetector(catalog,{**o['profile'],'jig_detection':self.settings});provider=Mock();provider.predict.return_value=rows;d.region_model=provider
        try:
            with patch('so101_teach.vision_service.detect') as legacy_cv:
                first=d.process(frame);second=d.process(frame.copy());self.assertIsNotNone(first['live_by_jig']['pallet']['selected']);self.assertIsNotNone(second['live_by_jig']['pallet']['selected']);self.assertEqual(provider.predict.call_count,2);legacy_cv.assert_not_called()
                provider.predict.return_value=[];lost=d.process(frame);self.assertIsNone(lost['by_jig']['pallet']['selected']);self.assertEqual(lost['live_by_jig']['pallet']['status'],'model_not_found')
        finally:d.close()
    def test_distinct_pallets_remain_ambiguous(self):
        c,frame,rows=self.inputs('C001');row=next(d for d in rows if d['class_id']==1);ds=[deepcopy(row),deepcopy(row)]
        def found(x):
            chosen={'center_px':[x,40],'area_px':1000,'score':.8,'metric':{'center_xy_mm':[x,40]}};return {'selected':chosen,'candidates':[chosen],'status':'shape_match'}
        with patch('so101_teach.cad_pallet.detect',side_effect=[found(20),found(100)]):raw=self.call('C001',rows=ds)
        self.assertIsNone(raw['selected']);self.assertEqual(raw['status'],'ambiguous');self.assertEqual(len(raw['candidates']),2)

if __name__=='__main__':unittest.main()
