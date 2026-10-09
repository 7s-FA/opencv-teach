import time,unittest,io
from types import SimpleNamespace
from unittest.mock import Mock,patch
from PIL import Image
from tests import test_ui as fixtures
from so101_teach.domain import Snapshot
class LiveUIResponseTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_fixed_remote_pose_does_not_queue_unneeded_ik(self):
        a=self.app;a.remote_mode=True;a.remote=SimpleNamespace(error=None,bundle_hash='test',rpc=Mock(),close=Mock())
        step=a.store.step(a.target,'fixed')
        with patch.object(a,'motion_request',return_value=7) as move:
            a.begin_execution('move',[step],None)
            self.assertIsNone(a.remote_plan_job)
            move.assert_called_once_with('move',[step['ticks']])
        a.remote.rpc.assert_not_called()
    def test_source_change_keeps_frame_until_fresh_replacement(self):
        a=self.app;a.session=SimpleNamespace(running=True,error=None,close=Mock())
        a.latest=Snapshot('follower',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
        a.last_rgb=Image.new('RGB',(4,4),'gray');a.fit_image(a.preview_canvas,a.last_rgb)
        before=a.preview_canvas.find_withtag('image');a.set_mode('live')
        self.assertEqual(a.preview_canvas.find_withtag('image'),before)
        self.assertIsNotNone(a.last_rgb)
    def test_live_adjust_idle_keeps_detection_frozen(self):
        a=self.app;a.live_adjust.owner=a;a.detector.freeze(True)
        a.stop_preview(quiet=True)
        self.assertTrue(a.detector.frozen)
        a.live_adjust.stop(halt=False);self.assertFalse(a.detector.frozen)
    def test_unchanged_live_controls_do_not_reconfigure_widgets(self):
        a=self.app;a.live_adjust.update_controls()
        with patch.object(a.move_btn,'configure',wraps=a.move_btn.configure) as configure:
            for _ in range(20):a.live_adjust.update_controls()
            configure.assert_not_called()

    def test_second_editor_keeps_live_source_while_adjusting(self):
        a=self.app;a.commit_target();a.open_second_editor(SimpleNamespace(y=a.steps.bbox(a.selected)[1]+4))
        b=a.second_editor;a.mode='live';a.live_adjust.owner=b
        with patch.object(a,'set_mode') as change:b.activate();change.assert_not_called()
        a.live_adjust.owner=None
    def test_burst_of_samples_draws_only_latest_without_dropping_notice(self):
        import queue
        from threading import Event
        a=self.app;s=SimpleNamespace(running=True,error=None,state='READ_ONLY',events=queue.Queue(),program_active=Event(),command_pending=Event(),close=Mock())
        a.session=s
        h={n:{'goal_ticks':v,'torque':0,'voltage_v':12.2,'temperature_c':38,'load_raw':0,'current_raw':0,'status':0} for n,v in a.target.items()}
        for i in range(12):
            q={**a.target,'shoulder_pan':a.target['shoulder_pan']+i}
            s.events.put(('sample',Snapshot('follower',q,h,time.monotonic(),time.time(),a.calibration.sha256,True,'fake')))
        s.events.put(('notice','중요 상태 기록'))
        self.root.after_cancel(a.job)
        with patch.object(a.current_vars['shoulder_pan'],'set',wraps=a.current_vars['shoulder_pan'].set) as rows,patch.object(a,'notice') as notice:
            a.poll();self.assertEqual(rows.call_count,1)
            notice.assert_any_call('중요 상태 기록')
        self.assertEqual(a.latest.ticks['shoulder_pan'],a.target['shoulder_pan']+11)
    def test_transition_frame_is_replaced_in_place_and_stale_data_is_hidden(self):
        a=self.app;a.session=SimpleNamespace(running=True,error=None,close=Mock())
        a.latest=Snapshot('follower',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
        a.last_rgb=Image.new('RGB',(8,8),'gray');a.fit_image(a.preview_canvas,a.last_rgb);image_id=a.preview_canvas.find_withtag('image')
        a.set_mode('live');out=io.BytesIO();Image.new('RGB',(8,8),'blue').save(out,format='PNG')
        a.renderer=SimpleNamespace(poll=lambda:('frame',1,out.getvalue(),a.render_context,time.monotonic()),close=Mock())
        self.root.after_cancel(a.render_job);a.render_tick()
        self.assertEqual(a.preview_canvas.find_withtag('image'),image_id);self.assertIsNone(a.pending_render_context)
        a.session.running=False;self.root.after_cancel(a.render_job);a.render_tick()
        self.assertEqual(a.preview_canvas.find_withtag('image'),image_id)
        self.assertIn('마지막 수신 화면 · 현재 위치 아님',a.preview_canvas.itemcget('stale','text'))

    def test_second_editor_stop_toggle_stays_enabled_while_moving(self):
        a=self.app;a.commit_target();a.open_second_editor(SimpleNamespace(y=a.steps.bbox(a.selected)[1]+4))
        b=a.second_editor;a.session=SimpleNamespace(running=True,state='MOVING',error=None,close=Mock())
        a.latest=Snapshot('follower',a.target.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
        a.live_adjust.owner=b;b.poll();self.assertNotIn('disabled',b.move_btn.state());a.live_adjust.owner=None
