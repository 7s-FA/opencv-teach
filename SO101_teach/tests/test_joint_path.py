import unittest
from so101_teach.joint_path import JointPath
from so101_teach.domain import JOINTS
from tests import test_motion as fixtures
class JointPathTests(unittest.TestCase):
    def points(self,values):return [dict.fromkeys(JOINTS,v)|{'gripper':2000} for v in values]
    def test_intermediate_velocity_shared_and_nonzero(self):
        p=self.points([1500,1550,1650,1800]);path=JointPath(p[0],p[1:],350)
        self.assertTrue(path.segments[0].stop);self.assertFalse(path.segments[1].stop)
        self.assertGreater(path.segments[1].exit['elbow_flex'],0)
        self.assertEqual(path.segments[1].exit,path.segments[2].entry)
    def test_knots_exact_and_no_joint_overshoot(self):
        p=self.points([1500,1530,1650,1700,1550]);path=JointPath(p[0],p[1:],350)
        for seg in path.segments:
            self.assertEqual(seg.sample(0),seg.start);self.assertEqual(seg.sample(1),seg.goal)
            for i in range(101):
                sample=seg.sample(i/100)
                for n in JOINTS:self.assertLessEqual(min(seg.start[n],seg.goal[n]),sample[n]);self.assertLessEqual(sample[n],max(seg.start[n],seg.goal[n]))
    def test_grip_changes_force_stop_on_both_sides(self):
        p=self.points([1500,1550,1600,1650,1700]);p[3]['gripper']=1800;p[4]['gripper']=1800
        path=JointPath(p[0],p[1:],300)
        self.assertTrue(path.segments[1].stop);self.assertTrue(path.segments[2].stop);self.assertTrue(path.segments[-1].stop)
    def test_direction_reversal_zero_velocity(self):
        p=self.points([1500,1600,1700,1500]);path=JointPath(p[0],p[1:],250)
        self.assertTrue(path.segments[1].stop);self.assertEqual(set(path.segments[1].exit.values()),{0.})
    def test_peak_velocity_and_acceleration_continuity(self):
        p=self.points([1500,1550,1650,1800]);path=JointPath(p[0],p[1:],350)
        for seg in path.segments:
            for i in range(101):self.assertLessEqual(max(abs(x) for x in seg.velocity(i/100).values()),350.0001)
            for at in (0,1):
                other=at+.00000001 if at==0 else at-.00000001
                acc=abs(seg.velocity(at)['elbow_flex']-seg.velocity(other)['elbow_flex'])/(.00000001*seg.duration)
                self.assertLess(acc,.05)
    def test_preview_shares_path_and_timing(self):
        from so101_teach.playback import Playback
        p=self.points([1500,1550,1650,1800]);player=Playback();player.begin(p[0],p[1:],now=0,rate=300);path=JointPath(p[0],p[1:],300)
        self.assertEqual(player.duration,path.duration)
        for t in (0,.3,.8,1.3):self.assertEqual(player.sample(t)[0],path.sample(t))
class JointPathMotionTests(unittest.TestCase):
    setUp=fixtures.MotionTests.setUp
    snapshot=fixtures.MotionTests.snapshot
    def run_path(self,grip=False):
        self.s.arm(self.bus,self.snapshot());targets=[{n:v+d for n,v in self.ref.middle.items()} for d in (20,50,90)]
        for t in targets:t['gripper']=self.ref.middle['gripper']
        if grip:targets[-1]['gripper']+=30
        self.s.request('play',targets)
        for _ in range(400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.completed_request_id:break
        self.assertEqual(self.s.completed_request_id,self.s.request_serial);return targets
    def test_via_point_passes_without_settle_dwell_but_final_settles(self):
        targets=self.run_path();events=self.s.report['step_arrivals'];self.assertEqual([x['arrival_type'] for x in events],['settled','pass_through','settled'])
        self.assertEqual(self.s.last_goals,targets[-1])
    def test_grip_action_keeps_stop(self):
        self.run_path(True);self.assertTrue(all(e['arrival_type']=='settled' for e in self.s.report['step_arrivals']))

    def test_large_via_tracking_error_does_not_skip_to_next_target(self):
        self.s.arm(self.bus,self.snapshot());original=self.bus.write
        def write(reg,n,v,**kw):
            original(reg,n,v,**kw)
            if reg=='Goal_Position' and n=='shoulder_lift' and self.s.index==1:self.values[n]['Present_Position']=v+50
        self.bus.write=write
        targets=[{**self.ref.middle,'shoulder_lift':self.ref.middle['shoulder_lift']+d} for d in (20,70,120)]
        self.s.request('play',targets)
        for _ in range(400):
            self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
            if self.s.report.get('pauses'):break
        self.assertIsNone(self.s.completed_request_id);self.assertEqual(self.s.report['pauses'][-1]['target'],targets[1])
        self.assertFalse(any(e['arrival_type']=='pass_through' for e in self.s.report['step_arrivals']))
