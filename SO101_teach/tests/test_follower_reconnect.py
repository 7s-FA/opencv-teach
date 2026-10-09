import unittest
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear

class ReconnectTests(unittest.TestCase):
    def runner(self,link):
        self.events=[];self.control=cli.Control()
        return cli.Runner(link,self.events.append,self.control,120,linear=FakeLinear())
    def test_closed_old_session_reconnects_before_requiring_running(self):
        link=FakeLink();state=link.state;rpc=link.rpc;connected=[False]
        link.state=lambda:state() if connected[0] else {**state(),'running':False,'error':'previous session ended'}
        def call(method,args=None):
            result=rpc(method,args)
            if method=='connect':connected[0]=True
            return result
        link.rpc=call;runner=self.runner(link)
        runner.prepare_follower({},400)
        self.assertEqual([m for m,a in link.calls],['configure','connect','speed'])
        self.assertEqual(self.events,['FOLLOWER_CONNECTING','FOLLOWER_READY'])
        self.assertEqual(runner.follower_phase,'active')
    def test_new_session_startup_waits_for_fresh_sample(self):
        link=FakeLink();state=link.state;rpc=link.rpc;connected=[False];polls=[0]
        def read():
            value=state()
            if not connected[0]:return {**value,'running':False}
            polls[0]+=1
            if polls[0]<3:return {**value,'running':False,'latest':None}
            return value
        def call(method,args=None):
            result=rpc(method,args)
            if method=='connect':connected[0]=True
            return result
        link.state=read;link.rpc=call;runner=self.runner(link)
        with patch.object(cli.time,'sleep'):runner.prepare_follower({},400)
        self.assertGreaterEqual(polls[0],3);self.assertFalse(any(m=='command' for m,a in link.calls))
    def test_new_connection_error_is_not_suppressed(self):
        link=FakeLink();state=link.state;rpc=link.rpc;connected=[False]
        link.state=lambda:{**state(),'running':False,'error':'USB unavailable' if connected[0] else None}
        def call(method,args=None):
            result=rpc(method,args)
            if method=='connect':connected[0]=True
            return result
        link.rpc=call;runner=self.runner(link)
        with self.assertRaisesRegex(RuntimeError,'USB unavailable'):runner.prepare_follower({},400)
        self.assertNotIn('FOLLOWER_READY',self.events);self.assertEqual(runner.follower_phase,'active')
        self.assertFalse(any(m in ('speed','command') for m,a in link.calls))
    def test_connection_timeout_never_arms_or_prepares_linear(self):
        clock=[100.];link=FakeLink();state=link.state
        link.state=lambda:{**state(),'running':False,'error':None}
        with patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            runner=self.runner(link)
            with self.assertRaisesRegex(RuntimeError,'시간 초과'):runner.prepare_follower({},400)
        self.assertFalse(any(m=='command' for m,a in link.calls));self.assertEqual(runner.linear.calls,[])
    def test_active_connection_loss_still_fails_immediately(self):
        link=FakeLink();runner=self.runner(link);runner.prepare_follower({},400);state=link.state
        link.state=lambda:{**state(),'running':False}
        with self.assertRaisesRegex(RuntimeError,'팔로워 연결 종료'):runner.check()
    def test_shutdown_before_reconnect_sends_no_requests(self):
        link=FakeLink();runner=self.runner(link);self.control.shutdown.set()
        with self.assertRaises(cli.ShutdownRequested):runner.prepare_follower({},400)
        self.assertEqual(link.calls,[])
