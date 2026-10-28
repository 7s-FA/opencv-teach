from copy import deepcopy
from pathlib import Path
import json,tempfile,unittest
from unittest.mock import patch
from so101_teach.configuration import JigCatalog
from so101_teach.domain import ROOT,atomic_json
from so101_teach.execution_resources import execution_catalog,_catalogs

class StaticPreparationTests(unittest.TestCase):
    def setUp(self):_catalogs.clear()
    def test_bundle_path_rewrite_reuses_geometry_but_not_mutable_catalog(self):
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.configuration.JigCatalog',wraps=JigCatalog) as factory:
            a=execution_catalog(folder,{'calibration_file':'original.json'})
            b=execution_catalog(folder,{'calibration_file':'calibration/digest.json'})
            self.assertEqual(factory.call_count,1);self.assertEqual(a.items,b.items)
            self.assertEqual(b.floor_profile['calibration_file'],'calibration/digest.json')
            a.items['pallet']['name']='mutated'
            self.assertNotEqual(execution_catalog(folder,{}).items['pallet']['name'],'mutated')
    def test_geometry_profile_and_source_changes_invalidate(self):
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.configuration.JigCatalog',wraps=JigCatalog) as factory:
            root=Path(folder);a=execution_catalog(root,{})
            execution_catalog(root,{'test_geometry':1});self.assertEqual(factory.call_count,2)
            atomic_json(root/'jigs.json',{**a.items,'pallet':{**a.items['pallet'],'name':'new'}})
            self.assertEqual(execution_catalog(root,{}).items['pallet']['name'],'new');self.assertEqual(factory.call_count,3)
    def test_same_size_same_mtime_source_change_is_not_reused(self):
        import os
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.configuration.JigCatalog',wraps=JigCatalog) as factory:
            root=Path(folder);raw=(ROOT/'assets/jigs/pallet.stl').read_bytes();mesh=root/'fixture.stl';mesh.write_bytes(raw)
            catalog=JigCatalog(root);atomic_json(root/'jigs.json',{'pallet':{**catalog.items['pallet'],'stl':str(mesh)}})
            first=execution_catalog(root,{});stamp=mesh.stat()
            mesh.write_bytes(b'changed header'.ljust(80,b' ')+raw[80:]);os.utime(mesh,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
            second=execution_catalog(root,{})
            self.assertNotEqual(first.mesh('pallet')['sha256'],second.mesh('pallet')['sha256'])
            self.assertEqual(factory.call_count,2)
    def test_cache_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            for n in range(7):execution_catalog(folder,{'revision':n})
            self.assertEqual(len(_catalogs),4)
