"""Skip startup repositioning only from verified current joint feedback."""
from copy import deepcopy
import io,unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from tests import test_chain_parallel_start as fixture
from tests.test_episode_cli import cli
from tests.test_work_monitor import monitor
from so101_teach.domain import JOINTS
from so101_teach.motion import BODY_ARRIVAL_TOLERANCE_TICKS,ARRIVAL_TOLERANCE_TICKS

class StartSafeSkipTests(fixture.ChainParallelStartTests):
    def setUp(self):
        super().setUp()
        for arm,link in self.links.items():
            for step in self.episodes[arm]['steps']:
                value=next(iter(step['ticks'].values()));step['ticks']={n:value for n in JOINTS}
            link.position={n:1900 for n in JOINTS}
            state=link.state;rpc=link.rpc
            def reading(link=link,state=state):
                s=state();s['latest']['ticks']=deepcopy(link.position)
                s['latest']['telemetry']={n:s['latest']['telemetry'][str(i)] for i,n in enumerate(JOINTS)}
                return s
            def command(method,args=None,link=link,rpc=rpc):
                result=rpc(method,args)
                if method=='command' and args['action'] in ('move','play'):link.position=deepcopy(args['targets'][-1])
                return result
            link.state=reading;link.rpc=command
        self.shared['records']=cli.chain_start_records({a:(self.root,self.episodes[a],self.bundles[a],300,20) for a in self.links})

    def already_safe(self,*arms):
        for arm in arms:
            self.links[arm].mode='HOLD';self.links[arm].position=deepcopy(self.episodes[arm]['steps'][0]['ticks'])

    def test_both_already_safe_skip_both_reposition_commands_and_reuse_load_start(self):
        self.already_safe('arm2','arm3');self.build()
        self.assertFalse(any(action=='move' for _,action in self.trace))
        self.assertIn('CHAIN_SAFE_ARM_SKIPPED:arm2',self.events);self.assertIn('CHAIN_SAFE_ARM_SKIPPED:arm3',self.events)
        load=self.runner('arm3');load.prepared_start=self.shared['receipts']['arm3']
        load.execute(self.episodes['arm3'],self.bundles['arm3'],300,1.5)
        self.assertFalse(any(action=='move' for _,action in self.trace));self.assertIn(('arm3','play'),self.trace)

    def test_only_mismatching_arm_moves(self):
        self.already_safe('arm2');self.build()
        self.assertEqual([x for x in self.trace if x[1]=='move'],[('arm3','move')])
        self.assertTrue(self.shared['receipts']['arm2']['confirmed_without_move'])
        self.assertNotIn('confirmed_without_move',self.shared['receipts']['arm3'])

    def test_torque_off_matching_pose_still_requires_activation(self):
        self.already_safe('arm2','arm3')
        for link in self.links.values():link.mode='READ_ONLY'
        self.build()
        self.assertEqual([x for x in self.trace if x[1]=='arm'],[('arm2','arm'),('arm3','arm')])
        self.assertFalse(any(action=='move' for _,action in self.trace))

    def test_pose_drift_without_request_change_prevents_load_handoff(self):
        self.already_safe('arm2','arm3');self.build()
        self.links['arm3'].position['shoulder_pan']+=BODY_ARRIVAL_TOLERANCE_TICKS+1
        load=self.runner('arm3');load.prepared_start=self.shared['receipts']['arm3']
        with self.assertRaisesRegex(RuntimeError,'시작 안전자세가 변경'):
            load.execute(self.episodes['arm3'],self.bundles['arm3'],300,1.5)
        self.assertNotIn(('arm3','play'),self.trace)

    def test_skipped_arm_changed_during_other_dispatch_blocks_linear_start(self):
        self.already_safe('arm2');original=self.links['arm3'].rpc
        def dispatch(method,args=None):
            result=original(method,args)
            if method=='command' and args['action']=='move':self.links['arm2'].position['wrist_roll']+=BODY_ARRIVAL_TOLERANCE_TICKS+1
            return result
        with patch.object(self.links['arm3'],'rpc',side_effect=dispatch),self.assertRaisesRegex(RuntimeError,'이동 생략 후'):
            self.build()
        self.assertFalse(any(op=='ensure' for op,_ in self.linear.calls))
        self.assertFalse(any(action=='play' for _,action in self.trace))
        self.assertIn(('arm2','hold'),self.trace);self.assertIn(('arm3','hold'),self.trace)

    def test_independent_episode_skips_only_the_matching_initial_safe_step(self):
        self.already_safe('arm2');run=self.runner('arm2')
        run.execute(self.episodes['arm2'],self.bundles['arm2'],300,100.)
        self.assertFalse(any(action=='move' for _,action in self.trace))
        self.assertIn('SAFE_POSE_SKIPPED:ALREADY_AT_TARGET',self.events)
        self.assertIn(('arm2','play'),self.trace)

    def test_stale_matching_pose_cannot_skip_or_start_work(self):
        self.already_safe('arm2','arm3');self.links['arm3'].stale=True
        with self.assertRaisesRegex(ValueError,'정지 확인'):self.build()
        self.assertFalse(any(action in ('move','play') for _,action in self.trace))

    def test_explicit_resume_rechecks_skipped_pose_before_continuing(self):
        self.already_safe('arm2','arm3');run=self.runner('arm2');run.chain_start=self.shared;requested=[]
        def emit(event):
            self.events.append(event)
            if event=='CHAIN_SAFE_ARM_SKIPPED:arm3' and not requested:
                requested.append(True);self.control.command('estop')
            if event=='PAUSED':
                self.links['arm3'].position['wrist_roll']+=BODY_ARRIVAL_TOLERANCE_TICKS+1
                self.control.command('restart')
        run.emit=emit
        run.execute(self.episodes['arm2'],self.bundles['arm2'],300,100.)
        self.assertIn('PAUSED',self.events);self.assertIn('RESUMED',self.events)
        self.assertEqual([x for x in self.trace if x[1]=='move'],[('arm3','move')])

    def test_skip_predicate_preserves_feedback_and_arrival_limits(self):
        self.already_safe('arm2');target=self.episodes['arm2']['steps'][0]['ticks'];state=self.links['arm2'].state()
        self.assertTrue(cli.at_safe_pose(state,target))
        for joint,limit in [('shoulder_pan',BODY_ARRIVAL_TOLERANCE_TICKS),('gripper',ARRIVAL_TOLERANCE_TICKS)]:
            changed=deepcopy(state);changed['latest']['ticks'][joint]+=limit
            self.assertTrue(cli.at_safe_pose(changed,target));changed['latest']['ticks'][joint]+=1
            self.assertFalse(cli.at_safe_pose(changed,target))
        for variant in ('stale','moving','torque_off','calibration','missing','error','release','pending'):
            changed=deepcopy(state)
            if variant=='stale':changed['latest']['monotonic']-=.5
            if variant=='moving':changed['latest']['telemetry']['gripper']['moving']=1
            if variant=='torque_off':changed['latest']['telemetry']['gripper']['torque']=0
            if variant=='calibration':changed['latest']['calibration_matches']=False
            if variant=='missing':del changed['latest']['ticks']['gripper']
            if variant=='error':changed['error']='lost'
            if variant=='release':changed['release_pending']=True
            if variant=='pending':changed['command_pending']=True
            with self.subTest(variant=variant):self.assertFalse(cli.at_safe_pose(changed,target))

class TimingDisplayTests(unittest.TestCase):
    def test_short_timestamp_keeps_phase_summary_and_interrupted_marker(self):
        event={'at':1792648171.293,'arm':'arm3','status':'B_CHAIN_DONE','timing_summary':{'total_seconds':113.26,'completed':True,
          'phases':[{'phase':'build_prepare','seconds':19.361,'completed':True},{'phase':'build','seconds':49.703,'completed':False}]}}
        with redirect_stdout(io.StringIO()) as out:monitor.print_event(event)
        text=out.getvalue();self.assertRegex(text,r'^\[\d\d:\d\d:\d\d\]')
        self.assertIn('조립 전 준비 19.4초 / 조립 49.7초 (중단)',text);self.assertIn('소요 113.3초',text)
        self.assertNotIn('2026-',text)
    def test_skip_message_identifies_the_arm(self):
        shown=monitor.visible_event({'at':1,'arm':'arm2','status':'A_CHAIN_SAFE_ARM_SKIPPED:arm3'})
        self.assertIn('로봇팔3',shown['status']);self.assertIn('이동 생략',shown['status'])
