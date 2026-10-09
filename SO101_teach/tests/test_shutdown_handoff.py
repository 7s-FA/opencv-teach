import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_remote as fixture
from so101_teach.remote_client import RemoteLink


class HandoffTests(unittest.TestCase):
    setUp=fixture.RemoteTests.setUp
    tearDown=fixture.RemoteTests.tearDown
    rpc=fixture.RemoteTests.rpc

    def test_takeover_revokes_pc_commands_and_heartbeat(self):
        self.rpc('connect',{'speed':300})
        old=self.lease;new=self.rpc('handoff_shutdown',{'job_id':uuid.uuid4().hex})['value']['lease']
        self.assertNotEqual(old,new)
        for action in ('hold','release'):
            with self.assertRaises(ValueError):self.rpc('command',{'action':action})
        with self.assertRaises(ValueError):self.rpc('disconnect')
        with self.assertRaises(ValueError):self.runtime.detach(old)
        self.runtime.last_heartbeat=100
        self.runtime.heartbeat(old,True);self.assertEqual(self.runtime.last_heartbeat,100)
        self.runtime.heartbeat(new,True);self.assertGreater(self.runtime.last_heartbeat,100)
        self.assertFalse(self.runtime.detached)

    def test_pi_completion_releases_ownership_for_next_command(self):
        self.rpc('connect',{'speed':300})
        new=self.rpc('handoff_shutdown',{'job_id':uuid.uuid4().hex})['value']['lease']
        with self.assertRaises(ValueError):self.runtime.acquire()
        self.runtime.detach(new)
        next_lease=self.runtime.acquire()['lease'];self.assertNotEqual(new,next_lease)
        self.runtime.require_lease(next_lease)

    def test_pc_cleanup_after_ack_sends_no_disconnect_or_detach(self):
        link=object.__new__(RemoteLink)
        link.lease='old';link.error=None;link.stop=threading.Event();link.leader_client=None;link.tunnel=None
        listener=SimpleNamespace(failed=Mock());link.listeners={'follower':listener};link.record_transport=Mock();link.http=Mock();link.rpc=Mock()
        link.relinquish();link.close()
        self.assertIsNone(link.lease);self.assertTrue(link.stop.is_set())
        listener.failed.assert_called_once();link.http.assert_not_called();link.rpc.assert_not_called()
