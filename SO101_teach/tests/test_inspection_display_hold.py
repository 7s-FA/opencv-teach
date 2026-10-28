"""Result display duration is independent of inference latency and run handoff."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch
from tests import test_work_monitor_recording as fixture


class DisplayHoldTests(TestCase):
    setUp=fixture.MonitorRetentionRecordingUITests.setUp
    tearDown=fixture.MonitorRetentionRecordingUITests.tearDown
    panel=fixture.MonitorRetentionRecordingUITests.panel
    passed=fixture.MonitorRetentionRecordingUITests.passed

    def test_slow_classification_still_displays_for_two_full_seconds(self):
        p=self.panel();sample=self.passed();sample['visual']['age_s']=4.2;start=sample['received']
        with patch('desktop_monitor.ui.time.monotonic',return_value=start):p.consume(sample)
        self.assertEqual(p.hold_until,start+2);self.assertIn('2초 유지',p.camera_status.get())
        for elapsed in (.4,1.5,1.99):
            with patch('desktop_monitor.ui.time.monotonic',return_value=start+elapsed):
                p.paint();self.assertTrue(p.canvas.find_withtag('frame'));self.assertIn('판정 영상',p.camera_status.get())
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+2.01):p.paint()
        self.assertNotIn('2초 유지',p.camera_status.get())

    def test_handoff_keeps_old_frame_with_old_identity_until_expiry(self):
        p=self.panel();sample=self.passed();start=sample['received']
        with patch('desktop_monitor.ui.time.monotonic',return_value=start):p.consume(sample)
        next_run=deepcopy(sample);old=next_run['runs'][0];new={**old,'run_id':'b'*32,'arm':'arm3'}
        next_run['runs']=[new,old];next_run['control']['info'].update(run_id=new['run_id'],arm='arm3')
        next_run.update(visual={},inspection_frame=None,inspection_key=None,received=start+.4,frame_received=start+.4)
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+.4):p.consume(next_run)
        self.assertIn('직전 검사 결과 · 팔2',p.camera_status.get());self.assertEqual(p.hold_until,start+2)
        self.assertEqual(p.fields['arm'].get(),'팔3')
        view=p.recording_inspection(start+1)
        self.assertIn('직전 검사 결과 · 팔2',view['heading']);self.assertTrue(view['checks'])
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+2.01):p.paint()
        self.assertTrue(p.camera_status.get().startswith('실시간'))

    def test_next_result_waits_for_current_result_and_duplicate_does_not_rearm(self):
        p=self.panel();sample=self.passed();start=sample['received']
        with patch('desktop_monitor.ui.time.monotonic',return_value=start):p.consume(sample)
        second=deepcopy(sample);second['events'][-1]['at']+=.4;second['at']+=.4;second['received']+=.4
        second['inspection_key']=('v',2)
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+.4):p.consume(second)
        self.assertIs(p.held_inspection,sample);self.assertEqual(p.hold_until,start+2)
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+2.01):p.paint()
        self.assertIs(p.held_inspection,second);self.assertAlmostEqual(p.hold_until,start+4.01)
        duplicate=deepcopy(second);duplicate['inspection_key']=('v',3)
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+2.2):p.consume(duplicate)
        self.assertAlmostEqual(p.hold_until,start+4.01);self.assertIsNone(p.pending_inspection)

    def test_timer_starts_when_canvas_is_shown_not_when_result_is_received(self):
        p=self.panel();p.canvas.grid_remove();self.root.update();sample=self.passed();start=sample['received']
        with patch('desktop_monitor.ui.time.monotonic',return_value=start):p.consume(sample)
        self.assertEqual(p.hold_until,0);self.assertIsNotNone(p.pending_inspection)
        with patch('desktop_monitor.ui.time.monotonic',return_value=start+5):
            p.canvas.grid();self.root.update();p.paint()
        self.assertAlmostEqual(p.hold_until,start+7)

    def test_real_event_loop_keeps_previous_result_until_two_seconds(self):
        from pathlib import Path
        from PIL import ImageGrab
        import os,json
        p=self.panel();sample=self.passed();sample['visual']['age_s']=2.4
        p.consume(sample);start=time.monotonic();handed=False;captured=False
        while time.monotonic()-start<2.8:
            elapsed=time.monotonic()-start
            if elapsed>=.4 and not handed:
                value=deepcopy(sample);old=value['runs'][0];new={**old,'run_id':'b'*32,'arm':'arm3'}
                value['runs']=[new,old];value['control']['info'].update(run_id=new['run_id'],arm='arm3')
                value.update(visual={},inspection_frame=None,inspection_key=None,received=time.monotonic(),frame_received=time.monotonic())
                p.consume(value);handed=True
            if handed:p.snapshot['frame_received']=time.monotonic()
            self.root.update()
            if elapsed>=1.2 and not captured:
                self.assertIn('직전 검사 결과',p.camera_status.get())
                folder=Path(__file__).resolve().parents[1]/'verification/display-hold-fps12-20261027'
                ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(folder/'previous-result-retained.png');captured=True
            if p.camera_status.get().startswith('실시간'):
                observed=time.monotonic()-start;break
            time.sleep(.02)
        else:self.fail('Held result did not expire')
        self.assertGreaterEqual(observed,1.9);self.assertLess(observed,2.4)
        (folder/'hold-display-timing.json').write_text(json.dumps({'display_seconds':observed,'handoff_after_seconds':.4,
          'capture_age_on_result_seconds':2.4,'previous_result_label_verified':True,'robot_commands':0},indent=2)+'\n')
