"""Completed preview frames must not wait for the next scene request tick."""
import io
import queue
import time
import unittest
from PIL import Image
from so101_teach.preview import Renderer
from tests import test_ui as fixtures


class PreviewDeliveryTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def renderer(self):
        r=Renderer.__new__(Renderer)
        r.closed=False;r.token=1;r.pending=None;r.in_flight=1
        r.commands=queue.Queue(1);r.frames=queue.Queue(1);r.close=lambda:None
        return r

    def frame(self,context,requested_at=None):
        stream=io.BytesIO();Image.new('RGB',(32,18),'blue').save(stream,format='JPEG')
        return ('frame',1,stream.getvalue(),context,time.monotonic() if requested_at is None else requested_at)

    def test_completed_frame_displays_between_scene_request_ticks(self):
        a=self.app;a.set_mode('target');a.render_pose_available=True;a.renderer=self.renderer()
        self.root.after_cancel(a.render_job);a.render_job=None
        a.renderer.frames.put(self.frame(a.render_context));a.schedule_preview_poll()
        deadline=time.monotonic()+.2
        while not a.last_rgb and time.monotonic()<deadline:self.root.update();time.sleep(.002)
        self.assertEqual(a.displayed_token,1)
        self.assertIsNotNone(a.last_rgb)
        self.assertIsNone(a.render_poll_job)

    def test_pending_old_context_frame_does_not_replace_current_image(self):
        a=self.app;a.set_mode('target');a.render_pose_available=True;a.renderer=self.renderer()
        before=Image.new('RGB',(32,18),'red');a.last_rgb=before
        a.renderer.frames.put(self.frame(a.render_context-1));a.poll_preview_frame()
        self.assertIs(a.last_rgb,before)
        self.assertIsNone(a.renderer.in_flight)

    def test_expired_live_frame_retries_without_displaying_stale_pose(self):
        a=self.app;a.set_mode('live');a.render_pose_available=True;a.renderer=self.renderer()
        a.last_render=('previous request',);a.last_rgb=None
        a.renderer.frames.put(self.frame(a.render_context,time.monotonic()-1));a.poll_preview_frame()
        self.assertIsNone(a.last_rgb);self.assertIsNone(a.last_render)

    def test_hidden_workspace_drains_frame_without_painting(self):
        a=self.app;a.render_pose_available=True;a.renderer=self.renderer();a.workspace_visible=False
        a.renderer.frames.put(self.frame(a.render_context));a.poll_preview_frame()
        self.assertIsNone(a.last_rgb);self.assertIsNone(a.render_poll_job)


if __name__=='__main__':unittest.main()
