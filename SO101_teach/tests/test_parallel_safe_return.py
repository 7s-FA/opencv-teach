"""Both saved safe moves start before either completion, with shared controls."""
import unittest
from unittest.mock import patch
from tests import test_episode_recovery as fixture

cli=fixture.cli


class ParallelSafeReturnTests(unittest.TestCase):
    setUp=fixture.RecoveryTests.setUp
    run_reset=fixture.RecoveryTests.run_reset

    def exercise(self,*,pause=False,fail=False,stale=False,startup=False,resume_command='restart',pause_seconds=0.):
        clock=[100.];order=[];control=cli.Control();moved={};resumed=[False]
        original_rpc=self.Link.rpc;original_state=self.Link.state
        def rpc(link,method,args=None):
            args=args or {};index=self.links.index(link)
            if method=='command':
                order.append((index,args['action']))
                if args['action']=='move':
                    if fail and index==1:raise RuntimeError('arm3 return rejected')
                    link.auto_finish=False;moved[index]=moved.get(index,0)+1
            return original_rpc(link,method,args)
        def state(link):
            if len(moved)==2 and not fail:
                if pause and not resumed[0]:
                    if not control.pause.is_set():control.command('estop')
                elif all(n>=2 if pause else n>=1 for n in moved.values()):
                    link.pending=False;link.mode='HOLD';link.completed=link.request;link.index=1
            if stale and len(moved)==2:link.stale=True
            return original_state(link)
        def sleep(seconds):
            clock[0]+=seconds
            if pause and control.paused and not resumed[0]:
                clock[0]+=pause_seconds;resumed[0]=True;control.command(resume_command)
        with patch.object(self.Link,'rpc',rpc),patch.object(self.Link,'state',state),patch.object(cli,'Control',return_value=control),patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=sleep):
            code=self.run_reset(startup=startup)
        self.order=order;self.moved=moved
        return code

    def test_both_moves_are_dispatched_before_waiting_for_either_arrival(self):
        self.assertEqual(self.exercise(),0)
        actions=[pair for pair in self.order if pair[1] in ('arm','move')]
        self.assertEqual(actions,[(0,'arm'),(1,'arm'),(0,'move'),(1,'move')])

    def test_prepare_home_also_moves_both_before_linear_retraction(self):
        self.assertEqual(self.exercise(startup=True),0)
        self.assertEqual(self.moved,{0:1,1:1})
        self.assertEqual(self.linear.target,1.5)

    def test_second_dispatch_failure_holds_both_without_reporting_success(self):
        self.assertEqual(self.exercise(fail=True),1)
        self.assertIn((0,'hold'),self.order);self.assertIn((1,'hold'),self.order)
        self.assertEqual(fixture.recovery.reset_context(self.root,'arm2')['state'],'pending')
        self.assertEqual(fixture.recovery.reset_context(self.root,'arm3')['state'],'ready')

    def test_shared_pause_resume_reissues_both_safe_targets(self):
        self.assertEqual(self.exercise(pause=True),0)
        self.assertEqual(self.moved,{0:2,1:2})
        self.assertIn((0,'hold'),self.order);self.assertIn((1,'hold'),self.order)

    def test_stale_feedback_stops_both_without_success(self):
        self.assertEqual(self.exercise(stale=True),1)
        self.assertIn((0,'hold'),self.order);self.assertIn((1,'hold'),self.order)

    def test_reset_after_long_pause_restarts_both_without_expiring_move_budget(self):
        self.assertEqual(self.exercise(pause=True,resume_command='reset',pause_seconds=130.),0)
        self.assertEqual(self.moved,{0:2,1:2})
