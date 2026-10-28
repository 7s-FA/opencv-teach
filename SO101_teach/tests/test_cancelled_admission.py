"""Reset/clear intent fences an earlier job still waiting for receipt fsync."""
import unittest
from tests.test_wpf_bridge import BridgeTests as Fixture

class CancelledAdmissionTests(unittest.TestCase):
    setUp=Fixture.setUp
    connect=Fixture.connect
    request=Fixture.request
    def test_cancel_controls_do_not_allow_an_older_unsent_job_after_durability(self):
        for command in ('RESET','CLEAR_ERROR'):
            with self.subTest(command=command):
                self.setUp();writes=[];self.service.writer=lambda path,value,done:writes.append(done)
                self.assertTrue(self.service.submit(self.request())['accepted'])
                self.assertTrue(self.service.submit(self.request(command,'2'*32))['accepted'])
                writes[0](None);writes[1](None)
                self.assertEqual(self.arms['arm2'].sent,[(command,'2'*32)])
                self.assertEqual(self.service.submit(self.request())['code'],'EXISTING_REQUEST')
