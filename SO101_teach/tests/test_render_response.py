"""Latest-only queue and source generation tests; never open robot/camera ports."""
import queue
import unittest
from so101_teach.preview import Renderer,PREVIEW_FPS,frame_delay_ms

class RenderQueueTests(unittest.TestCase):
    def renderer(self):
        r=Renderer.__new__(Renderer);r.closed=False;r.token=0;r.pending=None;r.in_flight=None
        r.commands=queue.Queue(1);r.frames=queue.Queue(1);return r
    def test_slow_renderer_returns_completed_frame_while_coalescing_new_views(self):
        r=self.renderer();r.submit((0,)*6,(0,0,1),context=7);first=r.commands.get_nowait()
        for angle in range(1,25):r.submit((0,)*6,(angle,0,1),context=7)
        self.assertTrue(r.commands.empty());self.assertEqual(r.in_flight,1)
        r.frames.put(('frame',first[0],b'pixels',first[5],first[6]))
        self.assertEqual(r.poll()[1],1)  # completed frame survives newer pending work
        newest=r.commands.get_nowait();self.assertEqual(newest[0],25);self.assertEqual(newest[2],(24,0,1))
        self.assertIsNone(r.pending);self.assertEqual(r.in_flight,25)
    def test_full_transport_retries_the_final_request_without_new_input(self):
        r=self.renderer();r.commands.put('busy');r.submit((1,)*6,(0,0,1))
        self.assertIsNone(r.in_flight);self.assertIsNotNone(r.pending)
        r.commands.get();self.assertIsNone(r.poll());self.assertEqual(r.commands.get()[0],1)
    def test_closed_renderer_does_not_queue_more_frames(self):
        r=self.renderer();r.closed=True
        self.assertFalse(r.submit((0,)*6,(0,0,1)));self.assertTrue(r.commands.empty())

    def test_30fps_budget_accounts_for_work_and_never_spins_to_catch_up(self):
        self.assertEqual(PREVIEW_FPS,30)
        self.assertEqual(frame_delay_ms(10.,10.),34)
        self.assertEqual(frame_delay_ms(10.,10.010),24)
        self.assertEqual(frame_delay_ms(10.,10.050),1)
