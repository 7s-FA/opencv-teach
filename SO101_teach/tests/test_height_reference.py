import json,tempfile,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from so101_teach.height_reference import *
from so101_teach.configuration import JigCatalog
from so101_teach.vision_service import MultiDetector

class HeightReferenceTests(unittest.TestCase):
    def test_raised_arm_lowers_work_surface_in_arm_coordinates(self):
        for height,z in ((0,-2.4),(5,-7.4),(-5,2.6)):
            p={'table_z_mm':floor_from_adjustment(height)}
            self.assertAlmostEqual(p['table_z_mm'],z)
            self.assertAlmostEqual(floor_adjustment(p),height)
            self.assertAlmostEqual(height_from_base_z(100,p),102.4+height)
            self.assertAlmostEqual(detection_plane_z(support_plane_profile(p,{'support_height_mm':0}),{'rim_z_mm':20}),z+20)
    def test_legacy_absolute_plane_is_not_reinterpreted_as_an_offset(self):
        p={'table_z_mm':-2.4}
        self.assertAlmostEqual(floor_adjustment(p),0)
        self.assertAlmostEqual(floor_from_adjustment(floor_adjustment(p)),p['table_z_mm'])
        self.assertEqual(p,{'table_z_mm':-2.4})
    def test_invalid_arm_height_is_rejected(self):
        for value in ('abc',float('nan'),float('inf'),1000,-1000):
            with self.assertRaises(ValueError):floor_from_adjustment(value)
    def test_remote_runtime_uses_lowered_plane_without_device_commands(self):
        from tests.test_remote import RemoteTests
        case=RemoteTests();case.setUp()
        try:
            bundle=deepcopy(case.bundle);bundle['profile']=profile_with_arm_height(bundle['profile'],10)
            case.runtime.configure(bundle)
            self.assertAlmostEqual(case.runtime.profile['table_z_mm'],-12.4)
            self.assertAlmostEqual(case.runtime.detector.profile['table_z_mm'],-12.4)
            self.assertEqual(case.runtime.profile['extrinsics'],bundle['profile']['extrinsics'])
            self.assertIsNone(case.runtime.session);self.assertIsNone(case.runtime.camera)
        finally:case.tearDown()
    def test_height_change_keeps_common_camera_and_work_surface_fixed(self):
        from tests.fixtures import load_profile
        from so101_teach.arm_workspace import preview_profile,world_from_base
        from so101_teach.vision import project_plane
        p,_,_=load_profile();before=deepcopy(p);old=preview_profile(p)
        raised=profile_with_arm_height(p,floor_adjustment(p)+5);new=preview_profile(raised)
        self.assertEqual(p,before)
        self.assertAlmostEqual(new['table_z_mm'],old['table_z_mm'])
        np.testing.assert_allclose(new['extrinsics']['base_from_camera'],old['extrinsics']['base_from_camera'])
        self.assertAlmostEqual(world_from_base(raised)[2,3]-world_from_base(p)[2,3],5)
        xy=[[100,200],[170,270]]
        np.testing.assert_allclose(project_plane(xy,p,floor_z(p)+20),project_plane(xy,raised,floor_z(raised)+20),atol=.0001)
        self.assertEqual(profile_with_arm_height(raised,floor_adjustment(raised)),raised)
    def test_legacy_plane_only_restore_keeps_original_camera_geometry(self):
        from tests.fixtures import load_profile
        p,_,_=load_profile();saved={'table_z_mm':-5}
        old=restore_model_floor(p,saved)
        self.assertEqual(old['extrinsics'],p['extrinsics']);self.assertEqual(old['table_z_mm'],-5)
        new=restore_model_floor(p,{**saved,'height_reference':'arm_origin_up'})
        self.assertEqual(new['table_z_mm'],-5);self.assertNotEqual(new['extrinsics'],p['extrinsics'])
    def test_floor_and_five_mm_riser_share_internal_reference(self):
        profile={'table_z_mm':-7.4};mesh={'low_mm':[0,0,10],'rim_z_mm':30}
        for height,z in [(0,-7.4),(5,-2.4)]:
            config={'support_height_mm':height};plane=support_plane_profile(profile,config)
            self.assertAlmostEqual(support_bottom_z(config,profile),z)
            self.assertAlmostEqual(detection_plane_z(plane,mesh),z+20)
            self.assertAlmostEqual(height_from_base_z(z,profile),height)
        self.assertEqual(profile,{'table_z_mm':-7.4})
    def test_legacy_migration_preserves_physical_z_and_is_idempotent(self):
        p={'table_z_mm':-5};old={'support_z_mm':0,'roi':[[0,0],[1,0],[0,1]],'stl':'same.stl'}
        migrated=normalize_jig_height(old,p)
        self.assertEqual(migrated['support_height_mm'],5);self.assertNotIn('support_z_mm',migrated)
        self.assertEqual(support_bottom_z(migrated,p),0);self.assertEqual(normalize_jig_height(migrated,p),migrated)
        self.assertEqual(migrated['roi'],old['roi']);self.assertEqual(old['support_z_mm'],0)
    def test_empty_values_default_to_explicit_zero(self):
        for item in ({},{'support_z_mm':None},{'support_z_mm':''},{'support_height_mm':None},{'support_height_mm':''}):
            self.assertEqual(normalize_jig_height(item,{})['support_height_mm'],0.)
    def test_invalid_values_are_rejected(self):
        for v in ('abc',float('nan'),float('inf'),1501,-501):
            with self.assertRaises(ValueError):support_height({'support_height_mm':v},{})
    def test_catalog_migration_is_read_only_unless_requested(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'jigs.json';raw=json.dumps({'j':{'support_z_mm':-2.4,'name':'test','roi':[0,0,1,1]}});p.write_text(raw)
            c=JigCatalog(d,profile={'table_z_mm':-7.4});self.assertEqual(p.read_text(),raw);self.assertEqual(c.items['j']['support_height_mm'],5)
            JigCatalog(d,profile={'table_z_mm':-7.4},persist_migration=True)
            saved=json.loads(p.read_text());self.assertEqual(saved['j']['support_height_mm'],5);self.assertNotIn('support_z_mm',saved['j'])
            self.assertEqual(JigCatalog(d,profile={'table_z_mm':-20}).items,saved)
    def test_new_copy_and_reload_preserve_height(self):
        with tempfile.TemporaryDirectory() as d:
            c=JigCatalog(d);item=c.save({'name':'test','unit':'mm','shape':'rectangle','method':'edges','stl':None,'support_height_mm':''})
            self.assertEqual(item['support_height_mm'],0);item['support_height_mm']=5;c.save(item);copy=c.duplicate(item['id'])
            self.assertEqual(JigCatalog(d).items[copy['id']]['support_height_mm'],5)
    def test_multi_detector_uses_each_jigs_floor_relative_plane(self):
        profile={'table_z_mm':-7.4}
        mesh={'low_mm':[0,0,0],'rim_z_mm':20};catalog=SimpleNamespace(revision=0,items={'floor':{'support_height_mm':0},'riser':{'support_height_mm':5}},mesh=lambda key:deepcopy(mesh))
        detector=MultiDetector(catalog,profile,active='floor');seen=[]
        def detect(frame,mesh,roi,profile,**kw):
            seen.append(detection_plane_z(profile,mesh));return {'selected':None,'candidates':[]}
        with patch('so101_teach.vision_service.detect',side_effect=detect):detector.process(np.zeros((720,1280,3),np.uint8))
        np.testing.assert_allclose(seen,[12.6,17.6]);self.assertEqual(profile['table_z_mm'],-7.4)
