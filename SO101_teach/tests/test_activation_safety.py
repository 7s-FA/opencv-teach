"""Offline regressions for the three pre-deployment activation review findings."""
from copy import deepcopy
import unittest
from tests.test_activation_overlap import ActivationOverlapTests
from so101_teach.domain import JOINTS


class ActivationSafetyTests(unittest.TestCase):
    setUp=ActivationOverlapTests.setUp
    arm=ActivationOverlapTests.arm
    def snapshot(self):
        value=ActivationOverlapTests.snapshot(self)
        for name,h in value.telemetry.items():
            h['internal_goal_ticks']=self.values[name].get('Internal_Goal_Position',h['goal_ticks'])
            h['moving']=0
        return value
    def state(self):
        snapshot=self.snapshot()
        return {'running':True,'state':self.s.state,'error':None,'program_active':False,'command_pending':False,
            'activation_hold':{'goals':self.s.enabled.copy(),'since':self.s.activation_paused_at},
            'latest':{'ticks':snapshot.ticks,'telemetry':snapshot.telemetry,'monotonic':snapshot.monotonic,'calibration_matches':True}}
    def pause_after_gripper(self):
        asked=[]
        def hold(register,name):
            if 'gripper' in self.started and not asked:
                asked.append(len(self.writes));self.s.request('hold')
        self.on_read=hold;self.arm();self.on_read=None
        return asked[0]

    def test_off_joint_drift_uses_last_measured_position_not_old_sample(self):
        sampled=[];moved=[];self.auto_on=True
        def drift(reg,name):
            if name=='wrist_flex' and reg=='Present_Position' and not sampled:sampled.append(self.clock)
            if sampled and not moved and self.clock-sampled[0]>=.025 and self.values['wrist_flex']['Torque_Enable']==0:
                moved.append(self.clock);self.values['wrist_flex']['Present_Position']+=64
        self.on_read=drift;self.arm()
        self.assertTrue(moved)
        goal=next(v for n,r,v in self.writes if n=='wrist_flex' and r=='Goal_Position')
        self.assertEqual(goal,self.ref.middle['wrist_flex']+64);self.assertEqual(self.s.state,'HOLD')

    def test_movement_after_last_sample_blocks_goal_write(self):
        def drift(reg,name):
            if name=='gripper' and reg=='Present_Position' and self.s.activation_pending_goal:
                self.values[name]['Present_Position']+=64
        self.on_read=drift
        with self.assertRaisesRegex(RuntimeError,'자세·토크 변경'):self.arm()
        self.assertFalse(any(n=='gripper' and r=='Goal_Position' for n,r,v in self.writes))
        self.s.before_close(self.bus)

    def test_slow_goal_sample_never_enables_first_motor(self):
        self.io_seconds=.03
        with self.assertRaisesRegex(RuntimeError,'활성화 직전 위치 조회 지연'):self.arm()
        self.assertFalse(self.started)

    def test_goal_retry_revalidates_pose_before_implicit_enable(self):
        original=self.bus.write;failed=[];self.auto_on=True
        def fail(reg,name,value,**kwargs):
            if reg=='Goal_Position' and name=='gripper' and not failed:
                failed.append(1);self.values[name]['Present_Position']+=64
                raise ConnectionError('goal packet not applied')
            return original(reg,name,value,**kwargs)
        self.bus.write=fail
        with self.assertRaisesRegex(RuntimeError,'자세·토크 변경'):self.arm()
        self.assertEqual(set(self.started),{'wrist_roll'})
        self.assertFalse(any(n=='gripper' and r=='Goal_Position' for n,r,v in self.writes))
        self.s.before_close(self.bus)

    def test_successful_slow_monitor_is_rejected_before_next_goal_or_hold(self):
        def slow(reg,name):
            if len(self.started)>=4:self.io_seconds=.03
        self.on_read=slow
        with self.assertRaisesRegex(RuntimeError,'전체 상태 조회 지연'):self.arm()
        self.assertNotIn('elbow_flex',self.started);self.assertNotEqual(self.s.state,'HOLD')
        self.s.before_close(self.bus)

    def test_slow_final_scan_cannot_complete_activation(self):
        def slow(reg,name):
            if len(self.started)==6:self.io_seconds=.02
        self.on_read=slow
        with self.assertRaisesRegex(RuntimeError,'전체 상태 조회 지연'):self.arm()
        self.assertEqual(len(self.started),6);self.assertNotEqual(self.s.state,'HOLD')
        self.s.before_close(self.bus)

    def test_hold_preserves_enabled_subset_and_blocks_remaining_writes(self):
        index=self.pause_after_gripper()
        self.assertEqual(self.s.state,'ACTIVATION_PAUSED');self.assertEqual(set(self.started),{'wrist_roll','gripper'})
        self.assertEqual(self.writes[index:],[])
        self.assertEqual({n for n,v in self.values.items() if v['Torque_Enable']==1},{'wrist_roll','gripper'})
        for action in ('move','play','follow'):
            with self.assertRaises(ValueError):self.s.request(action,[self.ref.middle])

    def test_release_from_partial_activation_turns_off_all_and_clears_resume(self):
        self.pause_after_gripper();self.s.request('release');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'READ_ONLY');self.assertFalse(self.s.enabled)
        self.assertIsNone(self.s.activation_pending_goal);self.assertIsNone(self.s.activation_paused_at)
        self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))

    def test_partial_activation_does_not_drop_temperature_protection(self):
        from unittest.mock import patch
        self.pause_after_gripper()
        with patch.object(self.s.temperature,'observe',side_effect=RuntimeError('overheated')):
            with self.assertRaisesRegex(RuntimeError,'overheated'):self.s.on_snapshot(self.bus,self.snapshot())
        self.s.before_close(self.bus)

    def test_hold_during_implicit_enable_reconciles_enabled_joint_without_extra_writes(self):
        self.auto_on=True
        def hold(reg,name,value):
            if name=='gripper' and reg=='Goal_Position':self.s.request('hold')
        self.on_write=hold;self.arm()
        self.assertEqual(self.s.state,'ACTIVATION_PAUSED');self.assertEqual(set(self.s.enabled),{'wrist_roll','gripper'})
        self.assertFalse(any(r=='Torque_Enable' for n,r,v in self.writes))

    def test_hold_during_last_prewrite_read_blocks_implicit_enable(self):
        self.auto_on=True
        def hold(reg,name):
            if name=='gripper' and reg=='Torque_Enable' and self.s.activation_pending_goal:self.s.request('hold')
        self.on_read=hold;self.arm()
        self.assertEqual(set(self.s.enabled),{'wrist_roll'})
        self.assertFalse(any(n=='gripper' and r=='Goal_Position' for n,r,v in self.writes))

    def test_paused_snapshot_monitor_preserves_subset_and_no_auto_resume(self):
        self.pause_after_gripper();before=list(self.writes)
        for _ in range(10):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.writes,before);self.assertEqual(self.s.state,'ACTIVATION_PAUSED')
        self.values['wrist_roll']['Internal_Goal_Position']=self.ref.middle['wrist_roll']+9
        with self.assertRaisesRegex(RuntimeError,'서보 내부 목표'):self.s.on_snapshot(self.bus,self.snapshot())
        self.s.before_close(self.bus);self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))

    def test_explicit_arm_resumes_only_remaining_joints(self):
        self.pause_after_gripper();before=len(self.writes)
        self.s.request('arm');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'HOLD');self.assertEqual(len(self.started),6)
        self.assertFalse(any(n in ('wrist_roll','gripper') and r=='Goal_Position' for n,r,v in self.writes[before:]))

    def test_second_hold_cancels_queued_resume_without_more_activation(self):
        self.pause_after_gripper();before=list(self.writes)
        self.s.request('arm');self.s.request('hold');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'ACTIVATION_PAUSED');self.assertEqual(self.writes,before)
        self.assertFalse(self.s.command_pending.is_set());self.assertTrue(self.s.commands.empty())

    def test_hold_before_queued_arm_cancels_it_without_motor_writes(self):
        self.s.request('arm');self.s.request('hold');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertEqual(self.s.state,'READ_ONLY');self.assertFalse(self.writes)

    def test_hold_before_first_enable_and_release_from_partial_state(self):
        self.s.hold_requested.set();self.arm()
        self.assertEqual(self.s.state,'ACTIVATION_PAUSED');self.assertFalse(self.started)
        self.s.request('arm');self.s.on_snapshot(self.bus,self.snapshot());self.assertEqual(self.s.state,'HOLD')
        self.s.request('release');self.s.on_snapshot(self.bus,self.snapshot())
        self.assertTrue(all(v['Torque_Enable']==0 for v in self.values.values()))

    def test_partial_state_is_stopped_only_with_new_complete_matching_evidence(self):
        from tests.test_episode_cli import cli
        self.pause_after_gripper();state=self.state()
        self.assertTrue(cli.stopped(state));self.assertFalse(cli.settled(state));self.assertFalse(cli.torque_off(state))
        for variant in ('old','stale','moving','unexpected_on','internal','missing','unknown_goals'):
            changed=deepcopy(state)
            if variant=='old':changed['latest']['monotonic']=self.s.activation_paused_at-.001
            if variant=='stale':changed['latest']['monotonic']=self.clock-.6
            if variant=='moving':changed['latest']['telemetry']['gripper']['moving']=1
            if variant=='unexpected_on':changed['latest']['telemetry']['elbow_flex']['torque']=1
            if variant=='internal':changed['latest']['telemetry']['gripper']['internal_goal_ticks']+=9
            if variant=='missing':del changed['latest']['telemetry']['gripper']
            if variant=='unknown_goals':changed['activation_hold']['goals']['unknown']=1
            with self.subTest(variant=variant):self.assertFalse(cli.stopped(changed))

    def test_remote_hold_and_state_use_real_session_and_preserve_partial_status(self):
        from tests import test_remote
        fixture=test_remote.RemoteTests();fixture.setUp()
        try:
            fixture.runtime.session=self.s
            fired=[]
            def hold(reg,name):
                if 'gripper' in self.started and not fired:fired.append(1);fixture.rpc('command',{'action':'hold'})
            self.on_read=hold;self.arm();self.on_read=None;self.s.latest=self.snapshot()
            state=fixture.runtime.state(fixture.lease)['follower']
            self.assertEqual(state['state'],'ACTIVATION_PAUSED')
            self.assertEqual(set(state['activation_hold']['goals']),{'wrist_roll','gripper'})
            from tests.test_episode_cli import cli
            self.assertTrue(cli.stopped(state));self.assertFalse(cli.settled(state))
            fixture.rpc('command',{'action':'arm'});self.s.on_snapshot(self.bus,self.snapshot())
            self.assertEqual(self.s.state,'HOLD')
        finally:fixture.runtime.session=None;fixture.tearDown()

    def test_runner_wait_resumes_partial_activation_only_after_restart(self):
        from tests.test_episode_cli import cli,FakeLink
        from unittest.mock import Mock
        self.pause_after_gripper();state=self.state();link=FakeLink();run=cli.Runner(link,lambda _:None,cli.Control(),10)
        ready=link.state();ready['state']='HOLD'
        run.check=Mock(side_effect=[(state,True),(ready,False)])
        self.assertIs(run.wait(cli.settled,2),ready)
        self.assertEqual(link.calls,[('command',{'action':'arm'})])

    def test_paired_start_stops_before_other_arm_and_resumes_partial_activation(self):
        from tests.test_chain_parallel_start import ChainParallelStartTests
        from tests.test_episode_cli import cli
        import time
        fixture=ChainParallelStartTests();fixture.setUp()
        try:
            first=fixture.links['arm2'];original_rpc=first.rpc;original_state=first.state;paused=[];phase=[]
            class Events(list):
                def append(self,event):
                    super().append(event)
                    if event=='PAUSED':
                        phase.append('paused');fixture.control.resume.set()
            fixture.events=Events()
            def rpc(method,args=None):
                if method=='command' and args['action']=='hold' and first.mode=='ACTIVATION_PAUSED':return {}
                result=original_rpc(method,args)
                if method=='command' and args['action']=='arm' and not paused:
                    paused.append(time.monotonic());first.mode='ACTIVATION_PAUSED';fixture.control.pause.set()
                return result
            def state():
                value=original_state()
                if first.mode=='ACTIVATION_PAUSED':
                    value['latest']['ticks']=dict.fromkeys(JOINTS,2048)
                    value['latest']['telemetry']={n:{'moving':0,'torque':int(n=='wrist_roll'),'status':0,'goal_ticks':2048,'internal_goal_ticks':2048} for n in JOINTS}
                    value['activation_hold']={'goals':{'wrist_roll':2048},'since':paused[0]}
                return value
            first.rpc=rpc;first.state=state
            second=fixture.links['arm3'];second_rpc=second.rpc
            def other_rpc(method,args=None):
                if method=='command' and args['action']=='hold' and second.mode=='READ_ONLY':return {}
                if method=='command' and args['action']=='arm':self.assertEqual(phase,['paused'])
                return second_rpc(method,args)
            second.rpc=other_rpc
            fixture.build()
            self.assertEqual(sum(arm=='arm2' and action=='arm' for arm,action in fixture.trace),2)
            self.assertIn('CHAIN_SAFE_DONE',fixture.events)
        finally:fixture.doCleanups()
