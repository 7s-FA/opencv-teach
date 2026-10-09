import queue
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch

from so101_teach.safe_shutdown import SafeShutdown
from so101_teach.shutdown_receiver import wait_stable


class ShutdownTests(unittest.TestCase):
    def shutdown(self):
        manager=NS(root=NS(after=Mock()),apps={'arm2':NS(notice=Mock(),remote=NS(relinquish=Mock())),'arm3':NS(notice=Mock(),remote=NS(relinquish=Mock()))},
                   finish_safe_close=Mock(),pi_execution=None,active='arm2')
        job=SafeShutdown(manager);job.thread=object();return job,manager

    def test_acceptance_closes_before_motion_done_or_transport_exit(self):
        job,m=self.shutdown();job.events.put(('line','SHUTDOWN_ACCEPTED:job'));job.poll()
        m.finish_safe_close.assert_called_once();self.assertFalse(job.busy)
        for app in m.apps.values():app.remote.relinquish.assert_called_once()

    def test_failure_keeps_app_and_connections_open(self):
        job,m=self.shutdown();job.events.put(('line','SHUTDOWN_FAILED:arm3 복귀 실패'));job.events.put(('exit',1));job.poll()
        m.finish_safe_close.assert_not_called();self.assertFalse(job.busy)
        self.assertIn('arm3 복귀 실패',m.apps['arm2'].notice.call_args.args[0])

    def test_missing_done_does_not_disconnect(self):
        job,m=self.shutdown();job.events.put(('exit',0));job.poll();m.finish_safe_close.assert_not_called()

    def test_cancelled_close_does_not_finish(self):
        job,m=self.shutdown();job.cancelled=True;job.events.put(('line','SHUTDOWN_DONE'));job.events.put(('exit',0))
        job.poll();m.finish_safe_close.assert_not_called()

    def test_waits_for_active_job_to_finish_before_new_worker(self):
        job,m=self.shutdown();job.thread=None;m.pi_execution=NS(busy=True)
        with patch.object(job,'request') as request:job.poll();request.assert_not_called()
        self.assertTrue(job.busy)

    def stable(self,drift=False,bad=False,cancelled=False):
        clock=[0.];control=NS(shutdown=threading.Event());observations=[]
        if cancelled:control.shutdown.set()
        def state(arm):
            ticks={str(n):(10 if drift and arm=='arm3' and clock[0]>=2 else 0) for n in range(6)}
            observations.append((arm,clock[0]))
            return {'ok':not bad,'latest':{'monotonic':clock[0],'ticks':ticks}}
        links={a:NS(state=lambda a=a:state(a)) for a in ('arm2','arm3')}
        with patch('so101_teach.shutdown_receiver.time.monotonic',side_effect=lambda:clock[0]),patch('so101_teach.shutdown_receiver.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,round(clock[0]+seconds,3))):
            wait_stable(NS(settled=lambda s:s['ok']),links,control)
        return clock[0],observations

    def test_both_arms_stay_still_for_three_seconds(self):
        elapsed,seen=self.stable();self.assertGreaterEqual(elapsed,3);self.assertEqual({a for a,t in seen},{'arm2','arm3'})

    def test_drift_restarts_three_seconds_for_both_arms(self):
        elapsed,_=self.stable(drift=True);self.assertGreaterEqual(elapsed,5)

    def test_loss_of_hold_aborts_without_torque_release(self):
        with self.assertRaisesRegex(RuntimeError,'유지 확인'):self.stable(bad=True)

    def test_cancellation_aborts_dwell(self):
        with self.assertRaisesRegex(RuntimeError,'중단'):self.stable(cancelled=True)


class ReceiverTests(unittest.TestCase):
    def run_park(self,*,invalid=False,failed=False,check=False,autonomous=False):
        import tempfile
        from pathlib import Path
        from tests import test_episode_cli as fixture
        from so101_teach import pi_execution_receiver as adapter,shutdown_receiver as receiver
        from contextlib import ExitStack
        cli=fixture.cli;events=[];links=[];end=threading.Event()
        class Stream:
            def __iter__(self):end.wait(5);return iter(())
        class Link(fixture.FakeLink):
            def __init__(self,arm):super().__init__();self.arm=arm;self.stop=threading.Event();links.append(self)
            def heartbeat(self):self.stop.wait(2)
            def close(self):
                events.append(self.arm+':detach');self.stop.set()
                if hasattr(self,'thread'):self.thread.join(2)
            def rpc(self,method,args=None):
                if method=='execution_state':return fixture.FakeLink.state(self)
                if method=='handoff_shutdown':
                    events.append(self.arm+':handoff');return {'lease':'worker-'+self.arm,'instance':self.arm}
                if failed and self.arm=='arm3' and method=='command' and args['action']=='move':raise ValueError('arm3 blocked')
                result=super().rpc(method,args)
                if method=='command':events.append(self.arm+':'+args['action'])
                return result
            def post(self,endpoint,body=None):
                if endpoint=='/state':return {'instance':self.arm,'detached':False}
                return super().post(endpoint,body)
        class Linear(fixture.FakeLinear):
            def open(self):pass
            def close(self):pass
        def validate(cli,part):
            if invalid and part['arm']=='arm3':raise ValueError('bad arm3')
            ticks={str(i):2048 for i in range(6)}
            ep={'id':'episode','calibration_sha256':'cal','steps':[{'ticks':ticks,'safe_boundary':'start'},{'ticks':ticks,'safe_boundary':'end'}]}
            return None,ep,{},300.,120.
        request={'shutdown_id':'a'*32,'arms':{a:{'episode':{}} for a in ('arm2','arm3')},'links':{a:{'port':1,'token':'test','lease':'test','instance':a} for a in ('arm2','arm3')},'apply_jig':True}
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);(root/'data').mkdir()
            for target,name,value in [(cli,'ROOT',root),(cli,'Link',Link),(cli,'Control',cli.Control),(cli,'Status',cli.Status),(cli,'load_job',cli.load_job)]:
                stack.enter_context(patch.object(target,name,value))
            stack.enter_context(patch.object(adapter,'validate_snapshot',side_effect=validate))
            stack.enter_context(patch('linear_client.LinearClient',Linear))
            stack.enter_context(patch.object(receiver,'wait_stable',side_effect=lambda *args:events.append('three-second-dwell')))
            stack.enter_context(patch.object(adapter.signal,'signal'))
            try:receiver.park(cli,request,Stream(),events.append,check=check,autonomous=autonomous,on_accept=(lambda:events.append('accepted')) if autonomous else None)
            except ValueError as exc:events.append(str(exc))
            finally:end.set()
        return events,links

    def test_return_both_then_wait_then_release_both(self):
        events,links=self.run_park()
        self.assertLess(events.index('arm2:move'),events.index('arm3:move'))
        self.assertLess(events.index('arm3:move'),events.index('three-second-dwell'))
        for arm in ('arm2','arm3'):
            self.assertLess(events.index('three-second-dwell'),events.index(arm+':release'))
            self.assertLess(events.index(arm+':release'),events.index('SHUTDOWN_DONE'))

    def test_validate_both_before_any_device_write(self):
        events,links=self.run_park(invalid=True);self.assertEqual(links,[]);self.assertEqual(events,['bad arm3'])

    def test_failed_second_return_never_releases_either_arm(self):
        events,_=self.run_park(failed=True)
        self.assertFalse(any(':release' in event for event in events));self.assertNotIn('SHUTDOWN_DONE',events)

    def test_check_only_opens_no_devices(self):
        events,links=self.run_park(check=True);self.assertEqual(links,[]);self.assertEqual(events,['SHUTDOWN_CHECK_OK'])

    def test_pi_takes_both_leases_before_ack_and_moves_after_ack(self):
        events,links=self.run_park(autonomous=True)
        for arm in ('arm2','arm3'):
            self.assertLess(events.index(arm+':handoff'),events.index('accepted'))
            self.assertLess(events.index('accepted'),events.index(arm+':move'))
            self.assertLess(events.index(arm+':release'),events.index(arm+':detach'))
        self.assertTrue(all(link.stop.is_set() for link in links))


class GuiShutdownTests(unittest.TestCase):
    from tests import test_arm_workspaces as fixture
    setUp=fixture.ArmWorkspacesTests.setUp
    tearDown=fixture.ArmWorkspacesTests.tearDown

    def test_shutdown_from_read_only_enables_hold_button(self):
        self.fixture.ArmWorkspacesTests.connected(self,self.a,'READ_ONLY')
        self.a.hold_btn.state(['disabled'])
        self.manager.safe_shutdown=SafeShutdown(self.manager)
        with patch.object(self.manager.safe_shutdown,'poll'):
            self.manager.safe_shutdown.start()
        self.assertNotIn('disabled',self.a.hold_btn.state())
        self.a.session.close.assert_not_called()

    def test_close_keeps_window_alive_and_heartbeat_running(self):
        a=self.a;s=self.fixture.ArmWorkspacesTests.connected(self,a)
        with patch('so101_teach.safe_shutdown.SafeShutdown.start'):
            self.manager.close();self.manager.close()
        s.close.assert_not_called();self.assertFalse(a.closed);self.assertTrue(self.manager.safe_shutdown.busy)
        a.root.after_cancel(a.job)
        with patch.object(a,'poll_extended') as extended:
            s.heartbeat=0;a.poll();extended.assert_not_called();self.assertGreater(s.heartbeat,0)
        callback=Mock();a.guard(callback);callback.assert_not_called()
        a.stop_all();s.request.assert_called_once_with('hold',None)
        self.assertTrue(self.manager.safe_shutdown.cancelled)
