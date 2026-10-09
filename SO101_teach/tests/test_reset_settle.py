import threading,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from so101_teach.idle_release import IdleRelease

class ResetSettleTests(unittest.TestCase):
    def session(self,at=0,tick=2048):
        snap=SimpleNamespace(monotonic=at,ticks={str(i):tick for i in range(6)},telemetry={str(i):{'moving':0} for i in range(6)},calibration_matches=True,fresh=lambda now:0<=now-at<.5)
        return SimpleNamespace(running=True,state='HOLD',error=None,latest=snap,program_active=threading.Event(),command_pending=threading.Event(),request=Mock())
    def tick(self,timer,session,at,tick=2048):
        session.latest=self.session(at,tick).latest;return timer.poll(session,at)
    def test_idle_release_waits_three_seconds(self):
        s=self.session();timer=IdleRelease();timer.schedule(s)
        self.assertFalse(self.tick(timer,s,0));self.assertFalse(self.tick(timer,s,2.99));self.assertTrue(self.tick(timer,s,3))
        s.request.assert_called_once_with('release')
    def test_movement_restarts_three_second_interval(self):
        s=self.session();timer=IdleRelease();timer.schedule(s)
        self.tick(timer,s,0);self.tick(timer,s,2,2051)
        self.assertFalse(self.tick(timer,s,4.99,2051));self.assertTrue(self.tick(timer,s,5,2051))
    def test_new_command_cancels_old_release(self):
        s=self.session();timer=IdleRelease();timer.schedule(s);self.tick(timer,s,0)
        timer.cancel();s.state='MOVING';self.tick(timer,s,5);s.request.assert_not_called()
    def test_stale_feedback_and_session_replacement_cannot_release(self):
        s=self.session();timer=IdleRelease();timer.schedule(s);self.tick(timer,s,0)
        self.assertFalse(timer.poll(s,4));s.request.assert_not_called()
        self.assertFalse(timer.poll(self.session(5),5));self.assertIsNone(timer.session)
