"""A new request must never publish the previous program's completed steps."""
import unittest
import threading
from unittest.mock import patch
from tests import test_motion as fixtures

class RequestProgressTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def test_new_play_progress_is_zero_during_speed_configuration(self):
        from tests.test_remote import RemoteTests
        self.s.arm(self.bus,self.snapshot());self.s.index=27;self.s.active_request_id=7;self.s.completed_request_id=7;self.s.request_serial=8
        remote=RemoteTests();remote.setUp();remote.runtime.session=self.s;self.s.latest=self.snapshot()
        seen=[];original=self.s.configure_motion_speed
        def configure(bus,**kwargs):
            seen.append(remote.runtime.state(remote.lease)['follower'])
            return original(bus,**kwargs)
        try:
            request=self.s.request('play',[{n:v+20 for n,v in self.ref.middle.items()}])
            with patch.object(self.s,'configure_motion_speed',side_effect=configure):self.s.on_snapshot(self.bus,self.snapshot())
            self.assertEqual(seen[0]['request_id'],request)
            self.assertEqual(seen[0]['index'],0)
            self.assertEqual(seen[0]['completed_request_id'],7)
            self.assertTrue(seen[0]['program_active'])
        finally:remote.runtime.session=None;remote.tearDown()

    def test_concurrent_status_during_configuration_then_real_completion(self):
        from tests.test_remote import RemoteTests
        self.s.arm(self.bus,self.snapshot())
        self.s.index=27;self.s.active_request_id=7;self.s.completed_request_id=7;self.s.request_serial=8
        remote=RemoteTests();remote.setUp();remote.runtime.session=self.s;self.s.latest=self.snapshot()
        entered=threading.Event();resume=threading.Event()
        original=self.s.configure_motion_speed
        def configure(bus,**kwargs):
            entered.set()
            if not resume.wait(3):raise RuntimeError('test configuration wait expired')
            return original(bus,**kwargs)
        request=self.s.request('play',[{n:v+12 for n,v in self.ref.middle.items()}])
        worker=None
        try:
            with patch.object(self.s,'configure_motion_speed',side_effect=configure):
                worker=threading.Thread(target=self.s.on_snapshot,args=(self.bus,self.snapshot()))
                worker.start()
                self.assertTrue(entered.wait(3))
                for _ in range(20):
                    state=remote.runtime.state(remote.lease)['follower']
                    self.assertEqual((state['request_id'],state['index'],state['completed_request_id']),(request,0,7))
                    self.assertTrue(state['program_active'])
                resume.set();worker.join(3);self.assertFalse(worker.is_alive())
            for _ in range(80):
                self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            state=remote.runtime.state(remote.lease)['follower']
            self.assertEqual((state['request_id'],state['index'],state['completed_request_id']),(request,1,request))
            self.assertEqual(state['state'],'HOLD');self.assertFalse(state['program_active'])
        finally:
            resume.set()
            if worker:worker.join(3)
            remote.runtime.session=None;remote.tearDown()
