import unittest,queue
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_ui as fixtures
class LeaderNoticeTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    def tearDown(self):
        self.app.leader_session=None;self.app.session=None;fixtures.UITests.tearDown(self)
    def setup_leader(self,state='HOLD'):
        a=self.app;a.session=SimpleNamespace(state=state);a.leader_session=SimpleNamespace(events=queue.Queue(),error=None,latest=None)
        return a
    def test_idle_voltage_and_upload_errors_do_not_replace_teaching_status(self):
        a=self.setup_leader();a.notice('스텝 저장 완료')
        for kind in ('notice','device_notice','error'):a.leader_session.events.put((kind,'리더 수신 지연'))
        a.poll_leader_status();self.assertEqual(a.message.get(),'스텝 저장 완료');self.assertEqual(a.device_message.get(),'리더 수신 지연')
    def test_idle_disconnection_is_only_visible_in_monitor(self):
        a=self.setup_leader();a.leader_session.error='USB disconnected';a.notice('에피소드 실행 완료');a.poll_leader_status()
        self.assertEqual(a.message.get(),'에피소드 실행 완료');self.assertIn('USB disconnected',a.device_message.get())
    def test_same_idle_error_is_reported_when_follow_becomes_active(self):
        a=self.setup_leader();a.leader_session.error='missing';a.poll_leader_status();a.session.state='FOLLOW'
        with patch.object(a,'notice') as notice:
            a.poll_leader_status();a.poll_leader_status();notice.assert_called_once()
    def test_follow_preparation_reports_missing_leader(self):
        a=self.setup_leader();a.camera_task={'kind':'follow'};a.leader_session.error='missing'
        with patch.object(a,'notice') as notice:a.poll_leader_status();notice.assert_called_once()
        a.camera_task=None
    def test_recovered_then_recurring_error_is_reported_again(self):
        a=self.setup_leader('FOLLOW');a.leader_session.error='missing'
        with patch.object(a,'notice') as notice:
            a.poll_leader_status();a.leader_session.error=None;a.poll_leader_status();a.leader_session.error='missing';a.poll_leader_status();self.assertEqual(notice.call_count,2)
