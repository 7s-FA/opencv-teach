import unittest
from tests import test_motion as fixtures
from so101_teach.motion import smooth_fraction,SPEED_PRESETS,velocity_register
class SCurveTests(unittest.TestCase):
    def test_endpoints_clamped_and_flat(self):
        self.assertEqual(smooth_fraction(-1),0);self.assertEqual(smooth_fraction(2),1)
        self.assertLess(smooth_fraction(.001),.000001);self.assertLess(1-smooth_fraction(.999),.000001)
    def test_monotonic_symmetric(self):
        points=[smooth_fraction(i/1000) for i in range(1001)]
        self.assertTrue(all(a<=b for a,b in zip(points,points[1:])))
        self.assertAlmostEqual(smooth_fraction(.25),1-smooth_fraction(.75))
    def test_peak_command_speed_does_not_exceed_preset(self):
        for rate in SPEED_PRESETS.values():
            duration=1.875*500/rate;dt=.001
            values=[500*smooth_fraction(i*dt/duration) for i in range(int(duration/dt)+2)]
            self.assertLessEqual(max(b-a for a,b in zip(values,values[1:]))/dt,rate+.01)
    def test_new_presets_and_firmware_ceiling(self):
        self.assertEqual(list(SPEED_PRESETS.values()),[300,350,400])
        self.assertEqual([velocity_register(v,12) for v in SPEED_PRESETS.values()],[400,450,500])
class SCurveMotionTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def test_reverse_and_forward_complete_at_all_three_speeds(self):
        self.s.arm(self.bus,self.snapshot())
        for rate in SPEED_PRESETS.values():
            for direction in (-1,1):
                self.s.set_speed(rate);goal={n:v+direction*100 for n,v in self.s.last_goals.items()};self.s.request('move',[goal])
                for _ in range(250):
                    self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
                    if self.s.completed_request_id==self.s.request_serial:break
                self.assertEqual(self.s.completed_request_id,self.s.request_serial);self.assertEqual(self.s.last_goals,goal)
    def test_cancel_midcurve_does_not_continue_trajectory(self):
        self.s.arm(self.bus,self.snapshot());self.s.request('move',[{n:v+150 for n,v in self.ref.middle.items()}])
        for _ in range(12):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.s.request('hold');self.s.on_snapshot(self.bus,self.snapshot());held=self.s.last_goals.copy()
        for _ in range(20):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.last_goals,held);self.assertEqual(self.s.state,'HOLD')
