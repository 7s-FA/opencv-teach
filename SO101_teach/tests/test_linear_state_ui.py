import queue,threading,time,unittest
from unittest.mock import Mock,patch
from dataclasses import replace
from tests import test_arm_workspaces as fixtures
from so101_teach.linear_state import decode_report

class FakeStream:
    def __init__(self,connection,source):
        self.connection=connection;self.source=source;self.results=queue.Queue(1);self.stop=threading.Event();self.start=Mock()
    def close(self):self.stop.set()
    def is_alive(self):return False

class LinearStateUITests(unittest.TestCase):
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    def test_one_subscription_updates_both_windows_and_survives_arm_switch(self):
        before=(self.data/'workcell-preview.json').read_bytes()
        with patch('so101_teach.linear_state.StateStream',side_effect=FakeStream) as factory:
            for a in (self.a,self.b):a.remote_mode=True;a.start_linear_state_query()
            self.assertEqual(factory.call_count,1);stream=self.manager.linear_state_query
            for pulse in (2000,1015):
                stream.results.put(decode_report({'pulse_us':pulse,'age_s':.01}));self.manager.poll_linear_state()
                for a in (self.a,self.b):self.assertEqual(a.workcell_preview['linear_stage']['stroke_mm'],(pulse-1000)/10)
                self.assertEqual(self.a.linear_status.get(),self.b.linear_status.get())
            self.manager.select('arm3');self.assertEqual(factory.call_count,1);self.assertIs(self.manager.linear_state_query,stream)
            self.assertIsNone(self.a.session);self.assertIsNone(self.b.session)
            self.assertEqual((self.data/'workcell-preview.json').read_bytes(),before)
    def test_stream_stops_only_when_both_arms_leave_remote_mode(self):
        with patch('so101_teach.linear_state.StateStream',side_effect=FakeStream):
            for a in (self.a,self.b):a.remote_mode=True;a.start_linear_state_query()
            stream=self.manager.linear_state_query
            self.a.remote_mode=False;self.a.stop_linear_state();self.assertFalse(stream.stop.is_set())
            self.b.remote_mode=False;self.b.stop_linear_state();self.assertTrue(stream.stop.is_set())
    def test_stopped_standalone_reader_cannot_restore_a_queued_old_state(self):
        app=self.a;manager=app.workspace_manager;app.workspace_manager=None
        try:
            app.linear_state_query=FakeStream({},{});app.linear_state_query.results.put(decode_report({'pulse_us':2000,'age_s':.1}))
            app.stop_linear_state();app.poll_linear_state();self.assertIn('수신 끊김',app.linear_status.get())
        finally:app.workspace_manager=manager
    def test_shutdown_stops_shared_reader(self):
        with patch('so101_teach.linear_state.StateStream',side_effect=FakeStream):
            self.a.remote_mode=True;self.a.start_linear_state_query();stream=self.manager.linear_state_query
            self.manager.close();self.assertTrue(stream.stop.is_set())
    def test_loss_and_recovery_update_footer_without_covering_preview(self):
        self.root.geometry('1920x1080');self.root.update()
        for a in (self.a,self.b):
            a.apply_linear_state(decode_report({'pulse_us':1015,'age_s':.01}));a.apply_linear_state(decode_report({'pulse_us':1015,'age_s':2}))
            self.assertIn('수신 끊김',a.linear_status.get());self.assertIn('마지막 후진',a.linear_status.get())
            a.apply_linear_state(decode_report({'pulse_us':2000,'age_s':.01}));self.assertIn('전진 목표',a.linear_status.get())
            self.assertFalse(a.preview_canvas.winfo_children())
