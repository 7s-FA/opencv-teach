import time,unittest
from copy import deepcopy
from pathlib import Path
from PIL import ImageGrab
from tests import test_ui as fixture
from so101_teach.domain import ROOT,read_json


class FixedJigSettingsUITests(unittest.TestCase):
    setUp=fixture.UITests.setUp
    tearDown=fixture.UITests.tearDown
    def test_fixed_policy_and_model_are_visible_without_choice_or_manual_fields(self):
        a=self.app;s=a.settings;self.root.geometry('1920x1043')
        carrier=a.catalog.save({'name':'운반용 지그','stl':str(ROOT/'assets/carrier_assembly/carrier_assembly.stl'),'support_height_mm':0.,'roi':[.1,.1,.9,.9]})
        episode=deepcopy(a.episode);s.refresh_jigs();a.show_page('settings');s.tabs.select(s.pages['jigs']);a.render_enabled=True
        for key,label in [(carrier['id'],'직사각형'),('pallet','도넛형')]:
            s.jig_choice.current(s.jig_keys.index(key));s.fill_jig();pane=s.model_views['jigs'];deadline=time.monotonic()+15
            path=a.catalog.items[key]['stl']
            while not (pane.image is not None and pane.spec and pane.spec.get('stl')==path) and time.monotonic()<deadline:self.root.update();time.sleep(.02)
            self.assertIsNotNone(pane.image,pane.caption.get());self.assertIn(label,s.jig_policy.get());self.assertIn('mm',s.jig_policy.get())
            for name in ('jig_unit','jig_shape','jig_method','jig_width','jig_depth','jig_rim'):self.assertNotIn(name,s.vars)
            def controls(widget):
                return [widget]+[child for w in widget.winfo_children() for child in controls(w)]
            texts=[str(w.cget('text')) for w in controls(s.pages['jigs']) if 'text' in w.keys()]
            self.assertNotIn('새 지그',texts);self.assertNotIn('복사',texts)
            self.root.update();self.assertLessEqual(s.jig_detection_note.winfo_rooty()+s.jig_detection_note.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            out=ROOT/'verification/fixed-jig-settings-20261012';ImageGrab.grab((self.root.winfo_rootx(),self.root.winfo_rooty(),self.root.winfo_rootx()+self.root.winfo_width(),self.root.winfo_rooty()+self.root.winfo_height())).save(out/('settings-carrier.png' if key==carrier['id'] else 'settings-pallet.png'))
        a.render_enabled=False;s.jig_choice.current(s.jig_keys.index(carrier['id']));s.fill_jig();s.vars['jig_z'].set('2');s.save_jig()
        self.assertEqual(a.catalog.items[carrier['id']]['roi'],carrier['roi']);self.assertEqual(a.catalog.items[carrier['id']]['support_height_mm'],2.)
        self.assertEqual(a.episode,episode);self.assertEqual(a.catalog.items[carrier['id']]['method'],'grid')
    def test_missing_stl_draft_cannot_replace_registered_geometry(self):
        a=self.app;s=a.settings;before=deepcopy(a.catalog.items)
        s.vars['jig_stl'].set('')
        with self.assertRaisesRegex(ValueError,'STL'):s.save_jig()
        self.assertEqual(a.catalog.items,before);self.assertIsNone(a.session)
