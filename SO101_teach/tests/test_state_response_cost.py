from copy import deepcopy
import time,unittest
from tests import test_remote as fixtures
from so101_teach.motion import SPEED_PRESETS

class StateResponseTests(unittest.TestCase):
    setUp=fixtures.RemoteTests.setUp
    tearDown=fixtures.RemoteTests.tearDown
    rpc=fixtures.RemoteTests.rpc
    def test_execution_read_has_identical_follower_and_new_stop_and_sample(self):
        self.rpc('connect',{'speed':SPEED_PRESETS['보통']})
        expected=self.runtime.state(self.lease)['follower'];actual=self.rpc('execution_state')['value']
        self.assertEqual(actual,expected)
        self.runtime.stop_generation+=1;self.runtime.session.state='HOLD'
        self.assertEqual(self.rpc('execution_state')['value'],self.runtime.state(self.lease)['follower'])
    def test_heartbeat_does_not_copy_events_or_touch_follower_readiness(self):
        class Event(dict):
            def __deepcopy__(self,memo):raise AssertionError('unused history copied')
        self.runtime.events.append(Event(id=999,value='history'))
        before=time.monotonic();reply=self.runtime.state(self.lease,True,heartbeat_only=True)
        self.assertGreaterEqual(self.runtime.last_heartbeat,before);self.assertFalse(reply['detached'])
        self.assertNotIn('follower',reply);self.assertNotIn('events',reply)
        self.assertIsNone(self.rpc('execution_state')['value'])
    def test_execution_read_still_requires_lease_and_does_not_refresh_it(self):
        self.runtime.last_heartbeat=time.monotonic()-3
        old=self.runtime.last_heartbeat
        with self.assertRaisesRegex(ValueError,'끊겨'):self.rpc('execution_state')
        self.assertEqual(self.runtime.last_heartbeat,old)
