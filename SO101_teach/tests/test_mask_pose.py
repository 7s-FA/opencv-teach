from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
import io,unittest,json,zlib,base64
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile
from so101_teach.inference_protocol import encode_mask,decode_mask
from so101_teach.mask_pose import detect_mask_seeded
from tests.test_model_regions import SETTINGS

class MaskTransportTests(unittest.TestCase):
    def test_lossless_non_byte_aligned_mask(self):
        mask=np.random.default_rng(3).integers(0,2,(37,53),dtype=np.uint8).astype(bool)
        np.testing.assert_array_equal(decode_mask(encode_mask(mask),list(mask.shape)),mask)
    def test_invalid_truncated_extra_and_bomb_data_rejected(self):
        mask=np.ones((10,10),bool);encoded=encode_mask(mask)
        invalid=['!bad',encoded[:-4],base64.b64encode(zlib.compress(b'0'*10000)).decode(),
                 base64.b64encode(zlib.compress(b'0'*13)+b'extra').decode()]
        for value in invalid:
            with self.subTest(value=value[:12]),self.assertRaises(ValueError):decode_mask(value,[10,10])
        for size in ([0,10],[True,10],[100000,100000]):
            with self.assertRaises(ValueError):decode_mask(encoded,size)

class MaskPoseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/model-mask-seeds';cls.expected=json.loads((cls.folder/'expected.json').read_text())['expected']
        cfg=json.loads((ROOT/'tests/fixtures/vision-carrier-sequence/fixture.json').read_text());cls.profile=cfg['profile']
        cls.catalog=JigCatalog(ROOT/'data',profile=cls.profile);cls.catalog.items=cfg['jigs']
        for jig in cls.catalog.items.values():jig['stl']=str(ROOT/'data/jig_assets'/Path(jig['stl']).name)
        cls.frame=cv2.imread(str(ROOT/'tests/fixtures/vision-carrier-sequence/0.png'))
    def rows(self,model='n'):
        data=np.load(self.folder/(model+'-0.npz'))
        return [{'class_id':int(c),'confidence':float(s),'box':b.tolist(),'mask':m} for c,s,b,m in zip(data['classes'],data['scores'],data['boxes'],data['masks'])]
    def call(self,key,*,model='n',frame=None,roi=None,mesh=None):
        item=self.catalog.items[key];profile=support_plane_profile(self.profile,item)
        return detect_mask_seeded(self.frame if frame is None else frame,mesh or self.catalog.mesh(key),item['roi'] if roi is None else roi,profile,self.rows(model),SETTINGS)
    def test_real_masks_preserve_evaluated_rim_center_heading_and_not_mask_center(self):
        for model in ('n','s'):
            for key in self.catalog.items:
                # Pallet CAD expectations use the current-workstation fixture
                # in test_cad_pallet; this reference remains carrier-only.
                if key=='pallet':continue
                with self.subTest(model=model,jig=key):
                    result=self.call(key,model=model);chosen=result['selected'];self.assertIsNotNone(chosen,result['status'])
                    expected=self.expected[model]['jigs'][key]
                    np.testing.assert_allclose(chosen['metric']['center_xy_mm'],expected['center_xy_mm'],atol=1e-5)
                    self.assertAlmostEqual(chosen['metric']['yaw_deg'],expected['yaw_deg'],places=4)
                    self.assertTrue(result['mask_seed_refined']);self.assertTrue(chosen['shape_match'])
                    if key=='pallet':self.assertGreater(np.linalg.norm(np.array(result['mask_seed']['center_xy_mm'])-chosen['metric']['center_xy_mm']),5)
                    else:self.assertTrue(chosen['observed_boundary_refined']);self.assertTrue(chosen['orientation_verified'])
    def test_carrier_mask_with_no_real_rim_never_leaves_adoptable_candidates(self):
        for key in self.catalog.items:
            if self.catalog.mesh(key).get('shape')!='rectangle':continue
            result=self.call(key,frame=np.zeros_like(self.frame));self.assertIsNone(result['selected']);self.assertEqual(result['candidates'],[])
    def test_cut_roi_and_wrong_size_still_reject(self):
        result=self.call('pallet',roi=[.2,.1,.3,.2]);self.assertIsNone(result['selected'])
        mesh=self.catalog.mesh('pallet');mesh['size_mm']=[100.,100.,25.]
        result=self.call('pallet',mesh=mesh);self.assertIsNone(result['selected']);self.assertEqual(result['candidates'],[])
    def test_box_only_or_wrong_frame_masks_are_rejected(self):
        for bad in ({'class_id':1,'confidence':.9,'box':[0,0,50,50]}, {'class_id':1,'confidence':.9,'box':[0,0,50,50],'mask':np.ones((5,5),bool)}):
            with self.assertRaises(ValueError):detect_mask_seeded(self.frame,self.catalog.mesh('pallet'),None,self.profile,[bad],SETTINGS)
    def test_distinct_valid_instances_remain_ambiguous(self):
        frame=np.zeros((100,200,3),np.uint8);rows=[{'class_id':1,'confidence':.9,'box':[10,10,60,60],'mask':np.ones((100,200),bool)}]*2
        def found(x):
            chosen={'center_px':[x,40],'area_px':1000,'score':.8,'metric':{'center_xy_mm':[x,40]}}
            return {'selected':chosen,'candidates':[chosen],'status':'shape_match'}
        seed={'center_xy_mm':[0,0],'yaw_deg':0}
        with patch('so101_teach.cad_pallet.detect',side_effect=[found(20),found(100)]):
            result=detect_mask_seeded(frame,{'shape':'ring'},None,{},rows,SETTINGS)
        self.assertIsNone(result['selected']);self.assertEqual(result['status'],'ambiguous');self.assertEqual(len(result['candidates']),2)
    def test_invalid_carrier_rim_is_not_adopted_over_time(self):
        from so101_teach.vision_service import MultiDetector
        d=MultiDetector(self.catalog,{**self.profile,'jig_detection':SETTINGS});d.region_model=Mock();d.region_model.predict.return_value=self.rows()
        try:
            for at in (100.,101.1,102.2,103.3,104.4):
                with patch('so101_teach.vision_service.time.monotonic',return_value=at):result=d.process(np.zeros_like(self.frame))
                self.assertTrue(all(v['selected'] is None for key,v in result['by_jig'].items() if self.catalog.mesh(key).get('shape')=='rectangle'))
        finally:d.close()
