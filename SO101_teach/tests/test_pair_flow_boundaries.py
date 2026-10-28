"""Paired recovery pauses/failures with actual coordination and fake devices."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from so101_teach.domain import JOINTS
import episode_recovery as recovery

SCENARIOS=('normal','skip','pause_arm','pause_hold_wait','pause_move','pause_callback',
           'peer_failure','stale_after_move','skipped_changed','shutdown_after_arm')

def exercise_pair(function,scenario):
    clock=[100.];trace=[];events=[];control=cli.Control();fired=[False];armed=set();callback_count=[0]
    def emit(event):events.append(event);trace.append(('event',event))
    class Link(FakeLink):
        def __init__(self,arm):super().__init__();self.arm=arm
        def state(self):
            value=super().state()
            value['latest']['ticks']=dict.fromkeys(JOINTS,2048)
            value['latest']['telemetry']={n:{'moving':0,'torque':int(self.mode!='READ_ONLY')} for n in JOINTS}
            if scenario=='pause_hold_wait' and len(armed)==2 and not fired[0]:
                fired[0]=True;control.command('estop')
            return value
        def rpc(self,method,args=None):
            if method=='command':
                action=args['action'];trace.append(('command',self.arm,action,deepcopy(args.get('targets'))))
                if scenario=='peer_failure' and self.arm=='arm3' and action=='move':raise RuntimeError('peer move failure')
            value=super().rpc(method,args)
            if method=='command':
                action=args['action']
                if action=='arm':armed.add(self.arm)
                if self.arm=='arm2' and not fired[0]:
                    if (scenario=='pause_arm' and action=='arm') or (scenario=='pause_move' and action=='move'):
                        fired[0]=True;control.command('estop')
                    elif scenario=='stale_after_move' and action=='move':fired[0]=True;links['arm3'].stale=True
                    elif scenario=='shutdown_after_arm' and action=='arm':fired[0]=True;control.shutdown.set()
            return value
    links={a:Link(a) for a in ('arm2','arm3')};linear=FakeLinear()
    skip=scenario in ('skip','skipped_changed')
    if skip:
        for link in links.values():link.mode='HOLD'
    records={a:{'safe_ticks':dict.fromkeys(JOINTS,2048),'bundle':{'profile':{'robot_id':a}}} for a in links}
    def on_started():
        callback_count[0]+=1;trace.append(('linear_callback',))
        if scenario=='pause_callback' and not fired[0]:fired[0]=True;control.command('estop')
        if scenario=='skipped_changed':links['arm2'].request=999
        return True
    def sleep(seconds):
        clock[0]+=seconds
        if clock[0]>110:raise AssertionError('coordination did not terminate')
        if control.paused and not control.resume.is_set():control.command('restart')
    previous=lambda:None;control.emergency=previous
    with TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(cli.time,'monotonic',lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=sleep):
        try:
            result=function(cli,records,links,linear,control,emit,active=None,first='arm2',startup=True,
                persist=False,prepare_active=True,active_speed=300,on_started=on_started,skip_matching=skip)
            outcome=('success',result)
        except Exception as exc:outcome=(type(exc).__name__,str(exc))
    return {'trace':trace,'events':events,'outcome':outcome,'callback_count':callback_count[0],
            'emergency_restored':control.emergency is previous}

class PairFlowBoundaryTests(TestCase):
    def test_pause_boundaries_resume_without_repeating_linear_callback(self):
        for case in ('pause_arm','pause_hold_wait','pause_move','pause_callback'):
            with self.subTest(case=case):
                r=exercise_pair(recovery.reset_pair,case)
                self.assertEqual(r['outcome'][0],'success');self.assertEqual(r['callback_count'],1)
                self.assertIn('PAUSED',r['events']);self.assertIn('RESUMED',r['events'])
                self.assertTrue(r['emergency_restored'])
    def test_failures_hold_both_arms_and_never_publish_completion(self):
        for case in ('peer_failure','stale_after_move','skipped_changed','shutdown_after_arm'):
            with self.subTest(case=case):
                r=exercise_pair(recovery.reset_pair,case)
                self.assertNotEqual(r['outcome'][0],'success')
                self.assertNotIn('ARM_RESET_DONE:arm2',r['events']);self.assertNotIn('ARM_RESET_DONE:arm3',r['events'])
                for arm in ('arm2','arm3'):self.assertIn(('command',arm,'hold',None),r['trace'])
                self.assertTrue(r['emergency_restored'])
    def test_valid_safe_pose_reuse_does_not_send_move(self):
        r=exercise_pair(recovery.reset_pair,'skip');self.assertEqual(r['outcome'][0],'success')
        self.assertFalse(any(x[0]=='command' and x[2]=='move' for x in r['trace']))
        self.assertEqual(r['callback_count'],1)
