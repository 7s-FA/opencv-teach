"""Stop preempts commands already waiting in the Pi runtime, without motors."""
import threading
from unittest import TestCase
from unittest.mock import patch
from tests.test_remote import RemoteTests as Fixture

class ObservedLock:
    def __init__(self,lock,entered):self.lock=lock;self.entered=entered
    def __enter__(self):self.entered.set();self.lock.acquire();return self
    def __exit__(self,*args):self.lock.release()

class StopAdmissionTests(TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    rpc=Fixture.rpc

    def test_queued_motion_cannot_cross_acknowledged_hold(self):
        self.rpc('connect',{'speed':350})
        original=self.runtime.lock;entered=threading.Event();result=[]
        # Hold the normal RPC lock as a slow configure/planning request would.
        with original:
            self.runtime.lock=ObservedLock(original,entered)
            worker=threading.Thread(target=lambda:result.append(self.rpc('command',{'action':'arm'})))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.rpc('command',{'action':'hold'})['ok'])
            finally:self.runtime.lock=original
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result[0]['ok'])
        self.assertTrue(self.runtime.session.commands.empty())

    def test_stop_during_evidence_write_prevents_late_play(self):
        self.rpc('connect',{'speed':350});self.runtime.session.state='HOLD'
        ticks=self.runtime.ref.middle.copy()
        def stop(_):self.assertTrue(self.rpc('command',{'action':'hold'})['ok'])
        with patch('so101_teach.assembly_evidence.invalidate_manual_motion',side_effect=stop):
            result=self.rpc('command',{'action':'play','targets':[ticks]})
        self.assertFalse(result['ok'])
        self.assertTrue(self.runtime.session.commands.empty())

    def test_delayed_network_request_needs_current_observed_stop_generation(self):
        self.rpc('connect',{'speed':350})
        before=self.runtime.state(self.lease)['follower']['stop_generation']
        self.assertTrue(self.rpc('command',{'action':'hold'})['ok'])
        # A new server handler can still carry an old client observation.
        for old in (before,True,'1',None):
            self.assertFalse(self.rpc('command',{'action':'arm','stop_generation':old})['ok'])
        current=self.runtime.state(self.lease)['follower']['stop_generation']
        self.assertTrue(self.rpc('command',{'action':'arm','stop_generation':current})['ok'])
        self.assertEqual(self.runtime.session.commands.qsize(),1)

    def test_stop_thread_cannot_refresh_cli_motion_permission(self):
        from tests.test_episode_cli import cli
        link=cli.Link('arm2');link.lease='fake'
        link.observe_motion_state({'stop_generation':5})
        stopped=threading.Thread(target=lambda:link.observe_motion_state({'stop_generation':6}))
        stopped.start();stopped.join()
        with patch.object(link,'post',return_value={}) as post:
            link.rpc('command',{'action':'play','targets':[]})
        self.assertEqual(post.call_args.args[1]['args']['stop_generation'],5)

    def test_gui_command_keeps_observed_generation_in_payload(self):
        from so101_teach.remote_client import RemoteLink
        from unittest.mock import Mock
        link=object.__new__(RemoteLink);link.error=None;link.lease='fake'
        link.latest_state={'stop_generation':3};link.http=Mock(return_value={})
        link.rpc('command',{'action':'arm'})
        self.assertEqual(link.http.call_args.args[1]['args']['stop_generation'],3)
        link.rpc('command',{'action':'hold'})
        self.assertNotIn('stop_generation',link.http.call_args.args[1]['args'])

    def test_pause_arriving_during_state_read_is_consumed_before_ready(self):
        from tests.test_episode_cli import cli,FakeLink
        control=cli.Control();runner=cli.Runner(FakeLink(),lambda _:None,control,10)
        reads=[]
        def reading():
            reads.append(True)
            if len(reads)==1:control.pause.set()
            return {'stop_generation':len(reads)}
        def resume():control.pause.clear()
        with patch.object(runner,'state',side_effect=reading),patch.object(runner,'await_pause',side_effect=resume) as pause:
            state,resumed=runner.check()
        self.assertTrue(resumed);pause.assert_called_once()
        self.assertEqual(state['stop_generation'],2)

    def test_local_cli_lost_arm_lease_cannot_start_linear(self):
        from tests.test_episode_cli import cli,FakeLinear
        self.rpc('connect',{'speed':350})
        link=cli.Link('arm2');link.lease=self.lease
        def transport(endpoint,body=None,**kwargs):
            if endpoint=='/state':return self.runtime.state((body or {}).get('lease'))
            result=self.runtime.rpc(body)
            if not result['ok']:raise RuntimeError(result['error'])
            return result['value']
        link.post=transport
        # The controller has a new owner, before the old heartbeat notices it.
        self.runtime.lease='replacement-owner'
        linear=FakeLinear();runner=cli.Runner(link,lambda _:None,cli.Control(),10,linear=linear)
        with self.assertRaisesRegex((ValueError,RuntimeError),'연결'):
            runner.start_linear(100.)
        self.assertFalse(linear.calls)

    def test_execution_state_does_not_wait_behind_planning_lock(self):
        completed=threading.Event();results=[]
        def read():
            try:results.append(self.rpc('execution_state'))
            finally:completed.set()
        with self.runtime.lock:
            worker=threading.Thread(target=read);worker.start()
            immediate=completed.wait(.3)
        worker.join(1)
        self.assertTrue(immediate)
        self.assertTrue(results[0]['ok'])
