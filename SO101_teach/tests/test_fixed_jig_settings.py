from copy import deepcopy
import json,tempfile,unittest
from pathlib import Path
from so101_teach.configuration import JigCatalog,fixed_jig_detection
from so101_teach.domain import ROOT
from so101_teach.vision import stl_profile


class FixedJigSettingsTests(unittest.TestCase):
    def test_supported_models_force_one_policy_despite_old_selection_values(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog=JigCatalog(temp)
            for file,shape,method in [('jigs/pallet.stl','ring','edges'),('carrier_assembly/carrier_assembly.stl','rectangle','grid')]:
                item=catalog.save({'name':'fixed','stl':str(ROOT/'assets'/file),'unit':'m','shape':'wrong','method':'combined','manual_size_mm':[999,999],'manual_rim_mm':99,'support_height_mm':0.,'roi':None})
                self.assertEqual([item[k] for k in ('unit','shape','method')],['mm',shape,method])
                self.assertNotIn('manual_size_mm',item);self.assertNotIn('manual_rim_mm',item)
                mesh=catalog.mesh(item['id']);original=stl_profile(ROOT/'assets'/file)
                for key in ('sha256','size_mm','low_mm','rim_z_mm'):self.assertEqual(mesh[key],original[key])
    def test_reopen_cleans_only_obsolete_options_and_preserves_pose_roi_and_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog=JigCatalog(temp);item=deepcopy(catalog.items['pallet'])
            item.update(unit='cm',shape='rectangle',method='combined',manual_size_mm=[1,2],manual_rim_mm=3,roi=[.1,.2,.8,.9],support_height_mm=5.,physical_installation={'carrier_underside_height_mm':195})
            catalog.path.write_text(json.dumps({'pallet':item}));before=catalog.path.read_bytes()
            loaded=JigCatalog(temp);self.assertEqual(catalog.path.read_bytes(),before)
            cleaned=JigCatalog(temp,persist_migration=True).items['pallet']
            self.assertEqual(cleaned['id'],item['id']);self.assertEqual(cleaned['stl'],item['stl']);self.assertEqual(cleaned['roi'],item['roi']);self.assertEqual(cleaned['support_height_mm'],5.)
            self.assertEqual(cleaned['physical_installation'],item['physical_installation']);self.assertEqual(cleaned['method'],'edges')
            for field in ('manual_size_mm','manual_rim_mm'):self.assertNotIn(field,cleaned)
    def test_mesh_cannot_switch_algorithm_through_stale_catalog_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog=JigCatalog(temp);catalog.items['pallet'].update(method='grid',shape='rectangle',unit='m')
            mesh=catalog.mesh('pallet');self.assertEqual((mesh['method'],mesh['shape'],mesh['unit']),('edges','ring','mm'))
    def test_unsupported_or_manual_geometry_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog=JigCatalog(temp);before=deepcopy(catalog.items)
            with self.assertRaisesRegex(ValueError,'STL'):catalog.save({'name':'manual','stl':None})
            mesh=stl_profile(ROOT/'assets/jigs/pallet.stl');mesh['size_mm']=[700,700,60]
            with self.assertRaisesRegex(ValueError,'mm'):fixed_jig_detection(mesh)
            self.assertEqual(catalog.items,before)
