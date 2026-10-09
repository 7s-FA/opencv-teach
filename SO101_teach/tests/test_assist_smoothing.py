import math,unittest
import numpy as np
from tests import test_leader_assist as fixtures
from so101_teach.leader_assist import MotionEnvelope,TARGETS

class EnvelopeTests(unittest.TestCase):
    def test_movement_fades_and_rest_recovers_monotonically_with_rate_bounds(self):
        e=MotionEnvelope()
        for _ in range(250):e.update(0,.02)
        self.assertGreater(e.scale,.99)
        values=[]
        for _ in range(100):values.append(e.update(25,.02))
        self.assertLess(values[-1],.31);self.assertTrue(np.all(np.diff(values)<=1e-12))
        self.assertLessEqual(max(abs(np.diff(values))),1.5*.02+1e-12)
        start=e.scale;recovery=[e.update(0,.02) for _ in range(200)]
        self.assertLess(recovery[0]-start,.01);self.assertGreater(recovery[-1],.99)
        self.assertTrue(np.all(np.diff(recovery)>=-1e-12));self.assertLessEqual(max(np.diff(recovery)),.5*.02+1e-12)
    def test_reversals_and_variable_cadence_do_not_create_switching_or_overshoot(self):
        e=MotionEnvelope()
        for i in range(800):
            dt=(.015,.025,.04)[i%3];speed=25 if i%60<30 else -25
            previous=e.scale;e.update(speed,dt)
            self.assertGreaterEqual(e.scale,.3);self.assertLessEqual(e.scale,1)
            self.assertLessEqual(e.scale-previous,.5*dt+1e-12)
            self.assertLessEqual(previous-e.scale,1.5*dt+1e-12)
        self.assertLess(e.scale,.31)
    def test_invalid_input_rejects_and_zero_time_does_not_change_output(self):
        e=MotionEnvelope();self.assertEqual(e.update(30,0),.3)
        for speed,dt in ((float('nan'),.02),(5,float('inf')),(5,-1)):
            with self.assertRaises(ValueError):e.update(speed,dt)

class AdaptiveSessionTests(unittest.TestCase):
    setUp=fixtures.LeaderAssistTests.setUp
    setup_rig=fixtures.LeaderAssistTests.setup_rig
    sample=fixtures.LeaderAssistTests.sample
    step=fixtures.LeaderAssistTests.step
    begin=fixtures.LeaderAssistTests.begin
    activate=fixtures.LeaderAssistTests.activate
    def test_per_joint_motion_reduces_real_cap_and_bias_then_restores_smoothly(self):
        self.activate()
        self.assertEqual(self.values['shoulder_lift']['Torque_Limit'],18)
        for _ in range(80):self.step(.05)
        self.assertEqual(self.values['shoulder_lift']['Torque_Limit'],60)
        before=len(self.writes);caps=[]
        for _ in range(40):
            self.values['shoulder_lift']['Present_Position']+=15
            self.step(.05);caps.append(self.values['shoulder_lift']['Torque_Limit'])
        self.assertEqual(self.s.assist_state,'ACTIVE');self.assertLessEqual(caps[-1],19)
        self.assertEqual(self.values['elbow_flex']['Torque_Limit'],60)
        self.assertLess(abs(self.s.bias['shoulder_lift']),12*.4)
        self.assertLessEqual(max(abs(np.diff(caps))),math.ceil(60*1.5*.05)+1)
        for _ in range(80):self.step(.05)
        self.assertEqual(self.values['shoulder_lift']['Torque_Limit'],60)
        self.assertFalse(any(r=='Torque_Enable' for _,r,_ in self.writes[before:]))
        self.s.stop_assist();self.step();self.assertTrue(self.s.report['leader_assist']['restored'])
        history=self.s.report['leader_assist']['adaptation_history'];self.assertTrue(history)
        self.assertLess(min(x['scale']['shoulder_lift'] for x in history),.31)
        self.assertTrue(all(self.values[n]['Torque_Enable']==0 for n in TARGETS))
    def test_single_tick_jitter_keeps_stationary_support(self):
        self.activate()
        for i in range(250):
            self.values['shoulder_lift']['Present_Position']+=1 if i%2 else -1
            self.step(.02)
        self.assertEqual(self.values['shoulder_lift']['Torque_Limit'],60)
        self.assertEqual(self.s.envelopes['shoulder_lift'].speed,0)
    def test_pre_activation_fault_is_preserved_in_report(self):
        self.values['shoulder_lift']['I_Coefficient']=1;self.begin();self.follow='RUN';self.step()
        self.assertIn('I=0',self.s.report['leader_assist_error']);self.assertFalse(self.writes)
