import json,time,unittest
from pathlib import Path
from . import test_ui as fixtures
from so101_teach.domain import ROOT

class LeftCameraMountUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_left_mount_render_and_cad_restore_keep_intrinsics_and_ticks(self):
        from copy import deepcopy
        from PIL import ImageGrab
        a=self.app;intrinsics=deepcopy(a.profile['intrinsics']);ticks=a.target.copy();a.settings.restore_extrinsics()
        self.assertEqual(a.profile['extrinsics']['mount_side'],'left');self.assertEqual(a.profile['intrinsics'],intrinsics);self.assertEqual(a.target,ticks)
        self.assertFalse(a.profile['extrinsics']['verified_for_robot_motion']);a.show_page('model');a.reset_view();a.render_enabled=True;a.last_render=None
        end=time.monotonic()+15
        while a.last_rgb is None and time.monotonic()<end:self.root.update();time.sleep(.05)
        self.assertIsNotNone(a.last_rgb);self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/left-camera-view-model.png')
        a.show_page('teach');a.last_render=None
        end=time.monotonic()+2
        while time.monotonic()<end:self.root.update();time.sleep(.05)
        ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/left-camera-view-teach.png')
