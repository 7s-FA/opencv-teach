"""Storage repair plus reconnect cannot turn a receipt into fresh execution."""
from unittest import TestCase
from tests.test_wpf_bridge import BridgeTests as Fixture

class StorageRecoveryTests(TestCase):
    setUp=Fixture.setUp
    connect=Fixture.connect
    request=Fixture.request
    def deferred_writes(self):
        writes=[];self.service.writer=lambda path,value,done:writes.append(done)
        return writes
    def reconnect(self):
        self.service.close(self.generation);self.connect()
    def test_failed_save_then_reconnect_and_disk_repair_never_reexecutes(self):
        writes=self.deferred_writes();self.service.submit(self.request())
        writes[0](OSError('disk full'));self.reconnect()
        self.assertEqual(self.service.submit(self.request())['code'],'EXISTING_REQUEST')
        self.service.maintenance();writes[-1](None)
        self.service.query(self.generation,'1'*32)
        self.assertFalse(self.arms['arm2'].sent)
        self.assertEqual(self.arms['arm2'].queries,[('BUILD_A','1'*32,False)])
    def test_urgent_stop_is_sent_once_despite_save_failure_and_reconnect(self):
        writes=self.deferred_writes();self.service.submit(self.request('EMER_STOP'))
        self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','1'*32)])
        writes[0](OSError('disk full'));self.reconnect()
        self.assertEqual(self.service.submit(self.request('EMER_STOP'))['code'],'EXISTING_REQUEST')
        self.service.maintenance();writes[-1](None)
        self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','1'*32)])
    def test_old_session_admission_cannot_interfere_with_new_arm_job(self):
        writes=self.deferred_writes();self.service.submit(self.request())
        self.reconnect();writes[0](None)
        self.assertFalse(self.arms['arm2'].sent)
        self.assertTrue(self.service.submit(self.request('LOAD_A','2'*32))['accepted']);writes[-1](None)
        self.service.query(self.generation,'1'*32)
        self.assertFalse(self.arms['arm2'].sent);self.assertTrue(self.arms['arm3'].busy)
        self.assertEqual(self.arms['arm3'].sent,[('LOAD_A','2'*32)])
