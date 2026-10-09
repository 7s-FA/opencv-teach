import unittest
from copy import deepcopy
from so101_teach.camera_display import display_results
from so101_teach.display_pose import DisplayPoses
from tests.test_display_pose import item

class CameraDisplayTests(unittest.TestCase):
    def test_interpolation_never_changes_measurements_or_capture(self):
        poses=DisplayPoses(.12);old={'jig':{'selected':item(0)}};new={'jig':{'selected':item(12)}};saved=deepcopy(new)
        display_results(poses,old,0);display_results(poses,new,1)
        shown=display_results(poses,new,1.06)['jig']['selected']
        self.assertAlmostEqual(shown['center_px'][0],6);self.assertEqual(shown['metric'],new['jig']['selected']['metric'])
        self.assertEqual(display_results(poses,new,1.06,raw=True),new);self.assertEqual(saved,new)
    def test_missing_or_large_moved_detection_does_not_leave_ghost(self):
        poses=DisplayPoses(.12);display_results(poses,{'jig':{'selected':item(0)}},0)
        moved=display_results(poses,{'jig':{'selected':item(100)}},1)
        self.assertEqual(moved['jig']['selected']['center_px'][0],100)
        self.assertEqual(display_results(poses,{},1.1),{});self.assertFalse(poses.states)
