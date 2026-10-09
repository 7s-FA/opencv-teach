import time,unittest
from copy import deepcopy
from . import test_ui as fixtures
from so101_teach.domain import Snapshot

class TeachingShortcutTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        # The fixture camera is a stub whose close() does not clear running.
        # Detach it so Tk is destroyed before the next native-keyboard test.
        self.app.camera=None
        fixtures.UITests.tearDown(self)
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def focus(self,widget=None):
        widget=widget or self.app.steps;widget.focus_force();self.pump();self.assertEqual(self.root.focus_get(),widget);return widget
    def pump(self):
        end=time.monotonic()+.055
        while time.monotonic()<end:self.root.update();time.sleep(.003)
    def tap(self,key='z',widget=None,state=0):
        w=widget or self.app.steps;w.event_generate('<KeyPress-'+key+'>',state=state);w.event_generate('<KeyRelease-'+key+'>',state=state);self.pump()

    def test_z_toggles_on_press_without_saving_and_shift_does_nothing(self):
        a=self.app;w=self.focus();before=deepcopy(a.episode)
        w.event_generate('<KeyPress-z>',state=0);self.assertTrue(a.follow_jig.get());w.event_generate('<KeyRelease-z>',state=0);self.pump()
        self.tap();self.assertFalse(a.follow_jig.get());self.tap('Shift_L');self.tap('Shift_R');self.assertFalse(a.follow_jig.get());self.assertEqual(a.episode,before)

    def test_auto_repeat_does_not_toggle_repeatedly(self):
        a=self.app;w=self.focus();w.event_generate('<KeyPress-z>',state=0)
        for _ in range(3):
            w.event_generate('<KeyRelease-z>',state=0);w.event_generate('<KeyPress-z>',state=0);self.root.update()
        self.assertTrue(a.follow_jig.get());w.event_generate('<KeyRelease-z>',state=0);self.pump();self.tap();self.assertFalse(a.follow_jig.get())

    def test_modifier_combinations_do_not_toggle_but_caps_lock_is_supported(self):
        a=self.app;self.focus()
        for state in (1,4,8,64,128):self.tap('Z' if state==1 else 'z',state=state);self.assertFalse(a.follow_jig.get())
        self.tap('Z',state=2);self.assertTrue(a.follow_jig.get())

    def test_z_types_normally_in_name_entry_and_does_not_toggle_in_numeric_input(self):
        a=self.app;a.step_name.set('');entry=self.focus(a.step_name_entry);self.tap(widget=entry)
        self.assertEqual(a.step_name.get(),'z');self.assertFalse(a.follow_jig.get())
        self.focus(a.spins['shoulder_pan']);self.tap(widget=a.spins['shoulder_pan']);self.assertFalse(a.follow_jig.get())

    def test_safe_edit_and_other_pages_ignore_z(self):
        a=self.app;self.focus();a.begin_safe_edit();self.tap();self.assertFalse(a.follow_jig.get());a.end_safe_edit()
        a.show_page('camera');self.focus(a.camera_canvas);self.tap(widget=a.camera_canvas);self.assertFalse(a.follow_jig.get())

    def test_focus_loss_resets_pressed_key(self):
        a=self.app;w=self.focus();w.event_generate('<KeyPress-z>',state=0);self.assertTrue(a.follow_jig.get())
        a.step_name_entry.focus_force();self.root.update();self.assertFalse(a._jig_key_down)
        self.focus();self.tap();self.assertFalse(a.follow_jig.get())

    def test_z_then_immediate_space_saves_live_ticks_and_first_jig_reference(self):
        a=self.app;self.fake_jig_camera((228,-138,67));w=self.focus()
        w.event_generate('<KeyPress-z>',state=0);w.event_generate('<KeyRelease-z>',state=0);self.assertEqual(a.episode['jig_references'],{})
        ticks=a.reference.middle.copy();ticks['shoulder_pan']+=7
        a.latest=Snapshot('follower',ticks,{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        w.event_generate('<KeyPress-space>',state=0);self.root.update()
        self.assertTrue(a.follow_jig.get());self.assertEqual(len(a.episode['steps']),1);self.assertEqual(a.episode['steps'][0]['ticks'],ticks);self.assertEqual(a.episode['steps'][0]['jig_reference']['pose'],[228,-138,67])
        self.assertIn('Space',a.shortcut_hint.cget('text'));self.assertIn('Z 지그 전환',a.shortcut_hint.cget('text'));self.assertNotIn('Shift',a.shortcut_hint.cget('text'))
