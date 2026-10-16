import unittest,tempfile,json
from pathlib import Path
from so101_teach.configuration import JigCatalog
from tests import test_ui as fixtures

class DetectionMethodTests(unittest.TestCase):
    def test_legacy_white_setting_loads_as_fixed_edges_without_losing_roi_or_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            cat=JigCatalog(temp);item={**cat.items['pallet'],'method':'white','roi':[.1,.2,.8,.9]}
            Path(temp,'jigs.json').write_text(json.dumps({'pallet':item}))
            cat=JigCatalog(temp);self.assertEqual(cat.items['pallet'],{**item,'method':'edges'})
            cat.save(cat.items['pallet'])
            self.assertEqual(json.loads(Path(temp,'jigs.json').read_text())['pallet']['method'],'edges')
    def test_removed_method_is_normalized_to_fixed_model_method(self):
        with tempfile.TemporaryDirectory() as temp:
            cat=JigCatalog(temp)
            cat.save({**cat.items['pallet'],'method':'white'})
            self.assertEqual(cat.items['pallet']['method'],'edges')

class DetectionMethodUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_detection_settings_are_read_only_and_model_defined(self):
        s=self.app.settings
        for name in ('jig_method','jig_shape','jig_unit'):
            self.assertNotIn(name,s.vars)
        self.assertFalse(hasattr(s,'new_jig'));self.assertFalse(hasattr(s,'copy_jig'))
        s.save_jig();self.assertEqual(self.app.catalog.items['pallet']['method'],'edges')
