from types import SimpleNamespace
from unittest.mock import Mock,patch
import queue,time,unittest
from tests import test_ui as fixtures
from so101_teach.domain import Snapshot,read_json
from so101_teach.motion import MotionSession

class LeaderAssistUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        self.app.session=None;self.app.leader_session=None;fixtures.UITests.tearDown(self)
    def ready(self):
        a=self.app;a.profile['mode']='leader'
        sample=Snapshot('follower',a.reference.middle.copy(),{},time.monotonic(),time.time(),a.calibration.sha256,True,'fake')
        a.session=SimpleNamespace(running=True,state='HOLD',latest=sample)
        a.leader_session=SimpleNamespace(begin_assist=Mock(),stop_assist=Mock(),assist_state='OFF',assist_error=None,assist_level='약하게')
        a.ui_heartbeat=time.monotonic();return a
    def test_follow_intent_prepares_local_assistance_but_requires_live_follow_state(self):
        a=self.ready();a.prepare_leader_assist();a.leader_session.begin_assist.assert_called_once()
        level,guard,offsets=a.leader_session.begin_assist.call_args.args
        self.assertEqual(level,'약하게');self.assertEqual(guard(),'WAIT');self.assertEqual(offsets,a.reference.radians)
        a.session.state='FOLLOW';self.assertEqual(guard(),'RUN')
        a.ui_heartbeat-=1;self.assertEqual(guard(),'DELAY');a.ui_heartbeat=time.monotonic()
        a.session=SimpleNamespace(running=True,state='FOLLOW',latest=None);self.assertEqual(guard(),'STOP')
    def test_transient_staleness_is_distinct_from_calibration_fault(self):
        from dataclasses import replace
        a=self.ready();a.session.state='FOLLOW';a.prepare_leader_assist()
        guard=a.leader_session.begin_assist.call_args.args[1];sample=a.session.latest
        a.session.latest=replace(sample,monotonic=time.monotonic()-1)
        self.assertEqual(guard(),'DELAY')
        a.session.latest=replace(sample,calibration_matches=False)
        self.assertEqual(guard(),'LOST')
        a.session.latest=sample;a.leader_session.assist_state='OFF';a.leader_session.assist_interruption='수신 지연'
        with patch.object(a,'motion_request') as motion:a.poll_leader_assist();motion.assert_not_called()
        self.assertIn('읽기 유지',a.leader_assist_status.get())
    def test_off_is_saved_and_does_not_prepare_any_motor_output(self):
        a=self.ready();a.leader_assist_choice.set('사용 안 함');a.save_leader_assist();a.prepare_leader_assist()
        a.leader_session.stop_assist.assert_called_once();a.leader_session.begin_assist.assert_not_called()
        self.assertEqual(read_json(self.data/'preferences.json')['leader_gravity_assist'],'사용 안 함')
    def test_increasing_strength_during_follow_is_rejected_but_off_is_allowed(self):
        a=self.ready();a.session.state='FOLLOW';a.leader_assist_choice.set('보통')
        with self.assertRaisesRegex(ValueError,'정지한 뒤'):a.save_leader_assist()
        self.assertEqual(a.leader_assist_choice.get(),'약하게');self.assertNotIn('leader_gravity_assist',a.preferences)
        a.leader_assist_choice.set('사용 안 함');a.save_leader_assist();a.leader_session.stop_assist.assert_called_once()
    def test_assistance_fault_requests_hold_once_and_status_does_not_duplicate(self):
        a=self.ready();a.session.state='FOLLOW';a.leader_session.assist_state='ACTIVE';a.connection.set('리더 따라가기')
        a.poll_leader_assist();a.poll_leader_assist();self.assertEqual(a.connection.get().count('리더 보조'),1)
        a.leader_session.assist_state='FAULT';a.leader_session.assist_error='온도 오류'
        with patch.object(a,'motion_request') as motion:
            a.poll_leader_assist();a.poll_leader_assist();motion.assert_called_once_with('hold')
        self.assertNotIn(' · 리더 보조 ',a.connection.get());self.assertIn('중단',a.leader_assist_status.get())
    def test_assist_controls_render_in_existing_scroll_form(self):
        from PIL import ImageGrab
        a=self.app;a.show_page('settings');a.settings.tabs.select(a.settings.pages['robot'])
        for size in ('1280x800','1180x760'):
            self.root.geometry(size);self.root.update()
            canvas=a.settings.scroll_canvases[str(a.settings.pages['robot'])];canvas.yview_moveto(1);self.root.update()
            widget=a.settings.leader_assist_choice
            self.assertGreater(widget.winfo_width(),100)
            self.assertGreaterEqual(widget.winfo_rooty(),canvas.winfo_rooty())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/leader-assist-'+size+'.png')
