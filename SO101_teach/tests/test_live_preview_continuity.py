"""Rendering reads telemetry without waiting for the slower widget refresh."""
import time
from dataclasses import replace
from unittest.mock import Mock
from tests import test_arm_workspaces as fixtures

import unittest
class LivePreviewContinuityTests(unittest.TestCase):
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    connected=fixtures.ArmWorkspacesTests.connected

    def test_both_latest_samples_render_before_monitor_queue_is_drained(self):
        a,b=self.a,self.b
        for app in (a,b):
            self.connected(app);app.set_mode('live')
            ticks={**app.latest.ticks,'shoulder_pan':app.latest.ticks['shoulder_pan']+50}
            app.session.latest=replace(app.latest,ticks=ticks,monotonic=time.monotonic())
        renderer=Mock();renderer.submit.return_value=True;renderer.poll.return_value=None
        a.renderer=renderer;a.render_enabled=True;a.root.after_cancel(a.render_job);a.render_tick()
        args=renderer.submit.call_args.args
        self.assertEqual(args[0],a.reference.angles(a.session.latest.ticks))
        self.assertEqual(args[2]['workcell']['other_arm_joint_angles_rad'],list(b.reference.angles(b.session.latest.ticks)))
        self.assertNotEqual(a.latest.ticks,a.session.latest.ticks)
        for app in (a,b):app.session.request.assert_not_called()

    def test_new_invalid_session_sample_cannot_reuse_old_valid_monitor_sample(self):
        a,b=self.a,self.b
        for app in (a,b):self.connected(app);app.set_mode('live')
        b.session.latest=replace(b.latest,calibration_matches=False,monotonic=time.monotonic())
        placement=self.manager.live_workcell(a)
        self.assertFalse(placement['other_arm_visible'])
    def test_short_gap_retains_last_image_with_explicit_stale_label(self):
        from PIL import Image
        a,b=self.a,self.b
        for app in (a,b):
            self.connected(app);app.set_mode('live')
            app.session.latest=replace(app.latest,monotonic=time.monotonic()-1)
        a.last_rgb=Image.new('RGB',(800,600),'white');a.fit_image(a.preview_canvas,a.last_rgb)
        self.assertTrue(a.preview_canvas.find_withtag('image'))
        a.live_visibility=(True,True);a.root.after_cancel(a.render_job);a.render_tick()
        self.assertTrue(a.preview_canvas.find_withtag('image'))
        self.assertIn('현재 위치 아님',a.preview_canvas.itemcget('stale','text'))
