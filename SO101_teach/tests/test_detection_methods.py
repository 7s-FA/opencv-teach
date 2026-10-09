import unittest,tempfile,json
from pathlib import Path
from so101_teach.configuration import JigCatalog
from tests import test_ui as fixtures

class DetectionMethodTests(unittest.TestCase):
    def test_legacy_white_setting_loads_as_combined_without_losing_roi_or_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            cat=JigCatalog(temp);item={**cat.items['pallet'],'method':'white','roi':[.1,.2,.8,.9]}
            Path(temp,'jigs.json').write_text(json.dumps({'pallet':item}))
            cat=JigCatalog(temp);self.assertEqual(cat.items['pallet'],{**item,'method':'combined'})
            cat.save(cat.items['pallet'])
            self.assertEqual(json.loads(Path(temp,'jigs.json').read_text())['pallet']['method'],'combined')
    def test_removed_method_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            cat=JigCatalog(temp)
            with self.assertRaises(ValueError):cat.save({**cat.items['pallet'],'method':'white'})

class DetectionMethodUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_only_supported_methods_are_offered_and_saved(self):
        s=self.app.settings
        def walk(w):
            yield w
            for c in w.winfo_children():yield from walk(c)
        choices=[w for w in walk(s.pages['jigs']) if w.winfo_class()=='TCombobox' and str(w.cget('textvariable'))==str(s.vars['jig_method'])]
        self.assertEqual(len(choices),1);self.assertEqual(tuple(choices[0]['values']),('색상+윤곽+돌출','윤곽+돌출 모서리','외곽+교차점'))
        for title,method in [('색상+윤곽','combined'),('윤곽 중심','edges')]:
            s.vars['jig_method'].set(title);s.save_jig();self.assertEqual(self.app.catalog.items['pallet']['method'],method)
