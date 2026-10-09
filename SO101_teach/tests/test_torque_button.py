import unittest,threading
from types import SimpleNamespace
from so101_teach.ui import App
from so101_teach.domain import JOINTS
class TorqueButtonTests(unittest.TestCase):
    def value(self,values=None,fresh=True,state='READ_ONLY',running=True,pending=False):
        s=SimpleNamespace(running=running,state=state,command_pending=threading.Event(),latest=SimpleNamespace(fresh=lambda:fresh,telemetry={n:{'torque':v} for n,v in zip(JOINTS,values or [0]*6)}))
        if pending:s.command_pending.set()
        return App.torque_release_available(SimpleNamespace(session=s))
    def test_confirmed_off_disabled(self):self.assertFalse(self.value())
    def test_any_on_enabled(self):self.assertTrue(self.value([0,0,0,1,0,0],state='HOLD'))
    def test_stale_off_enabled(self):self.assertTrue(self.value(fresh=False))
    def test_missing_motor_enabled(self):self.assertTrue(self.value([0]*5))
    def test_activation_pending_enabled(self):self.assertTrue(self.value(state='ACTIVATING'));self.assertTrue(self.value(pending=True))
    def test_disconnected_disabled(self):self.assertFalse(self.value(running=False));self.assertFalse(App.torque_release_available(SimpleNamespace(session=None)))
