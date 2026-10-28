"""Repeated boundary tests with fake devices, virtual time and real local locks/pipes.

No serial, camera, ROS, SSH, or physical actuator connection is created.
"""
import fcntl
import os
import queue
import threading
import unittest
from unittest.mock import Mock
from tests import test_prepared_standby as standby_fixture
from tests import test_checkpoint_resume as resume_fixture
from tests.test_episode_cli import cli, FakeLink
from so101_teach.episode_faults import InspectionRetry
from supervision_gate import SupervisionGate
from supervision_session import SupervisionHealth


class StandbyBoundaryTests(unittest.TestCase):
    def fixture(self):
        f=standby_fixture.RuntimeStandbyTests();f.setUp();return f

    @staticmethod
    def refresh(f,at):
        f.now[0]=at;f.sample.monotonic=at
        f.r.camera.preview_frame=(f.r.camera.preview_frame[0],at)
        f.r.camera.observation=(*f.r.camera.observation[:2],at)

    def test_expiry_and_request_order_matrix(self):
        # 200 cases: five offsets, both lock orders, twenty independent jobs.
        for repeat in range(20):
            for offset in (-.01,-.000001,0,.000001,.01):
                for first in ('request','expiry'):
                    with self.subTest(repeat=repeat,offset=offset,first=first):
                        f=self.fixture()
                        try:
                            old=f.ready();self.refresh(f,190+offset)
                            if first=='expiry':
                                f.r.poll_standby()
                                self.assertEqual(f.r.camera.close.call_count,int(offset>=0))
                            with (f.root/'data/episode-cli.lock').open('a') as work:
                                fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB)
                                self.refresh(f,194+repeat/1000)
                                f.r.poll_standby();f.r.poll_idle_release()
                                f.r.session.request.assert_not_called()
                                with f.r.lock:new=f.r.call('standby_begin',{})['generation']
                                self.assertIsNone(f.r.idle_release.session)
                                with self.assertRaises(ValueError):f.r.call('standby_complete',{'generation':old})
                                self.refresh(f,195+repeat/1000)
                                f.r.call('standby_complete',{'generation':new})
                                deadline=f.r.standby.until
                                self.assertEqual(deadline,f.now[0]+180)
                                f.r.standby_control({'operation':'status'})
                                self.assertEqual(f.r.standby.until,deadline)
                            f.r.poll_standby();f.r.poll_idle_release()
                            f.r.session.request.assert_not_called()
                            self.assertEqual(f.r.camera.close.call_count,int(first=='expiry' and offset>=0))
                        finally:f.doCleanups()

    def test_concurrent_expiry_and_new_request_keep_actuator_idle(self):
        # 40 actual thread/OS-flock races; BUSY is a valid rejected admission.
        for repeat in range(40):
            with self.subTest(repeat=repeat):
                f=self.fixture()
                try:
                    f.ready();self.refresh(f,190+repeat/1000000)
                    barrier=threading.Barrier(2);errors=[];admitted=[]
                    def expire():
                        try:barrier.wait(timeout=2);f.r.poll_standby()
                        except BaseException as error:errors.append(error)
                    def request():
                        try:
                            with (f.root/'data/episode-cli.lock').open('a') as work:
                                barrier.wait(timeout=2)
                                try:fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB)
                                except BlockingIOError:admitted.append(False);return
                                with f.r.lock:
                                    generation=f.r.call('standby_begin',{})['generation']
                                    f.r.call('standby_complete',{'generation':generation})
                                f.r.poll_idle_release();admitted.append(True)
                        except BaseException as error:errors.append(error)
                    threads=[threading.Thread(target=target,daemon=True) for target in (expire,request)]
                    for thread in threads:thread.start()
                    for thread in threads:thread.join(3)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                    self.assertEqual(errors,[]);self.assertEqual(len(admitted),1)
                    f.r.session.request.assert_not_called()
                    if admitted[0]:
                        self.assertIsNone(f.r.idle_release.session)
                        self.assertEqual(f.r.standby.until,f.now[0]+180)
                    self.assertLessEqual(f.r.camera.close.call_count,1)
                finally:f.doCleanups()


class ResumeBoundaryTests(unittest.TestCase):
    def fixture(self):
        f=resume_fixture.ResumeStateTests();f.setUp()
        f.runner.peer=FakeLink();f.runner.peer.mode='HOLD'
        real_state=f.link.state
        def state():
            if f.link.error:raise RuntimeError(f.link.error)
            return real_state()
        f.link.state=state
        return f

    def assert_no_motion(self,f):
        for link in (f.link,f.runner.peer):
            self.assertFalse(any(method=='command' and args['action'] in ('arm','move','play','release')
                                 for method,args in link.calls))
        self.assertFalse(any(op in ('move','ensure') for op,args in f.runner.linear.calls))

    def test_transport_loss_phase_and_source_matrix(self):
        # 240 cases: each transport before/during/at/after commit, twenty repeats.
        for repeat in range(20):
            for source_name in ('link','peer','linear'):
                for phase in ('validate','operation','commit','after_commit'):
                    with self.subTest(repeat=repeat,source=source_name,phase=phase):
                        f=self.fixture()
                        try:
                            source=getattr(f.runner,source_name);f.on_pause=f.restart
                            def disconnect():source.error=source_name+' disconnected';f.now[0]+=(repeat+1)/1000
                            def validate():
                                if phase=='validate':disconnect()
                            count=[0]
                            def operation():
                                count[0]+=1
                                if count[0]==1:raise InspectionRetry('initial')
                                if phase=='operation':disconnect()
                                return 'PASS'
                            def save():
                                if phase=='commit' and any(r['state']=='completed' for r in f.control.receipts.values()):disconnect()
                            f.control.save_receipts=save
                            if phase=='after_commit':
                                self.assertEqual(f.runner.retry_visual(operation,validate=validate),'PASS')
                                disconnect()
                                with self.assertRaises(RuntimeError):f.runner.check()
                                self.assertEqual(f.events.count('RESUMED'),1)
                            else:
                                with self.assertRaisesRegex(RuntimeError,'disconnected'):
                                    f.runner.retry_visual(operation,validate=validate)
                                self.assertNotIn('RESUMED',f.events)
                                self.assertNotIn('RECOVERY_RECHECK_PASSED',f.events)
                                self.assertTrue(f.control.paused)
                                self.assertNotEqual(f.control.receipts[f.receipts[0]]['state'],'completed')
                                if phase=='commit':self.assertEqual(f.control.receipts[f.receipts[0]]['state'],'failed')
                            self.assert_no_motion(f)
                        finally:f.doCleanups()

    @staticmethod
    def supervision(f):
        gate=SupervisionGate();ticket=gate.reserve(client_id='c'*32)
        f.control.job_id=ticket.job_id
        f.control.supervision_fd=os.dup(ticket.read_fd);os.set_blocking(f.control.supervision_fd,False)
        f.addCleanup(os.close,f.control.supervision_fd);f.addCleanup(ticket.close)
        health=SupervisionHealth.__new__(SupervisionHealth);health.gate=gate
        health.notifications=queue.Queue();health.fresh=lambda client:False
        return ticket,health

    @staticmethod
    def lose_lease(health):
        health.stop=Mock();health.stop.wait.side_effect=[False,True];health.watch()

    def test_supervision_loss_discards_old_retry_and_requires_explicit_new_retry(self):
        # 60 cases using the real watcher and OS pipe, including completion writes.
        for repeat in range(20):
            for phase in ('validate','operation','commit'):
                with self.subTest(repeat=repeat,phase=phase):
                    f=self.fixture()
                    try:
                        ticket,health=self.supervision(f);lost=[False]
                        def inject():
                            if not lost[0]:lost[0]=True;self.lose_lease(health)
                        def paused():
                            if not f.receipts:f.restart();return
                            self.assertTrue(ticket.link_lost)
                            self.assertNotIn('RESUMED',f.events)
                            self.assertEqual(f.control.receipts[f.receipts[0]]['state'],'cancelled')
                            self.assertEqual(f.restart('a')['state'],'cancelled')
                            self.assertFalse(f.control.resume.is_set())
                            health.fresh=lambda client:True
                            self.assertFalse(f.control.resume.is_set())
                            f.restart('b')
                        f.on_pause=paused
                        def validate():
                            if phase=='validate':inject()
                        calls=[0]
                        def operation():
                            calls[0]+=1
                            if calls[0]==1:raise InspectionRetry('initial')
                            if phase=='operation':inject()
                            return 'PASS'
                        def save():
                            if phase=='commit' and any(r['state']=='completed' for r in f.control.receipts.values()):inject()
                        f.control.save_receipts=save
                        self.assertEqual(f.runner.retry_visual(operation,validate=validate),'PASS')
                        self.assertEqual(f.events.count('PAUSED'),2);self.assertEqual(f.events.count('RESUMED'),1)
                        self.assertEqual(f.control.receipts['a'*32]['state'],'cancelled')
                        self.assertEqual(f.control.receipts['b'*32]['state'],'completed')
                        self.assert_no_motion(f)
                    finally:f.doCleanups()

    def test_supervisor_eof_rejects_new_resume(self):
        # 20 process-loss cases; closing the writer cannot become auto-resume.
        for repeat in range(20):
            with self.subTest(repeat=repeat):
                f=self.fixture()
                try:
                    ticket,_=self.supervision(f)
                    def paused():
                        if not f.receipts:f.restart();return
                        self.assertTrue(f.control.supervision_lost)
                        self.assertNotIn('RESUMED',f.events)
                        with self.assertRaisesRegex(ValueError,'SUPERVISION_LOST'):f.restart('b')
                        f.control.shutdown.set()
                    f.on_pause=paused;closed=[False]
                    def save():
                        if not closed[0] and any(r['state']=='completed' for r in f.control.receipts.values()):
                            closed[0]=True;os.close(ticket.write_fd);ticket.write_fd=None
                    f.control.save_receipts=save
                    with self.assertRaises(cli.ShutdownRequested):
                        f.runner.retry_visual(Mock(side_effect=[InspectionRetry('initial'),'PASS']))
                    self.assertEqual(f.control.receipts['a'*32]['state'],'cancelled');self.assert_no_motion(f)
                finally:f.doCleanups()
