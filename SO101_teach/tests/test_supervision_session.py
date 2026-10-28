"""Lease expiry, durable deduplication and uncertain-result regression tests."""
import json,os,tempfile,threading,time,uuid,queue,subprocess,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
from tests.test_action_contract import ActionContractTests as ActionFixture,Goal,Process
from tests.test_pc_action_client import PCActionClientTests as PCFixture
from supervision_session import RequestLedger,SupervisionHealth,decode_goal
from supervision_gate import SupervisionGate

class DurableRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ActionFixture.setUpClass()
    def setUp(self):
        self.f=ActionFixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.server=self.f.server
    def retry(self,goal,operation='execute'):
        again=Goal(goal.meta['command']);again.meta.update(goal.meta,attempt_id=uuid.uuid4().hex,operation=operation)
        again.request.command=json.dumps(again.meta);return again
    def test_new_ros_attempt_same_business_id_returns_result_once(self):
        goal=Goal('BUILD_A')
        with patch.object(self.f.module.subprocess,'Popen',return_value=Process(['A_DONE'])) as start:
            self.assertTrue(self.f.execute('arm2',goal).success)
            self.assertTrue(self.f.execute('arm2',self.retry(goal)).success)
            self.server.ledger=RequestLedger(self.f.module.ROOT,'new-server-epoch')
            self.assertTrue(self.f.execute('arm2',self.retry(goal)).success)
            start.assert_called_once()
    def test_query_fences_never_admitted_job_against_late_execute(self):
        goal=Goal('BUILD_A');query=self.retry(goal,'query')
        with patch.object(self.f.module.subprocess,'Popen') as start:
            self.assertEqual(self.f.execute('arm2',query).message,'NOT_FOUND')
            self.assertEqual(self.f.execute('arm2',goal).message,'NOT_FOUND');start.assert_not_called()
    def test_inflight_duplicate_and_restart_never_reexecute(self):
        goal=Goal('BUILD_A');self.assertTrue(self.server.goal_callback(goal.request))
        self.assertEqual(self.f.execute('arm2',self.retry(goal)).message,'PENDING')
        self.server.ledger=RequestLedger(self.f.module.ROOT,'different')
        self.assertEqual(self.f.execute('arm2',self.retry(goal,'query')).message,'UNKNOWN')
        self.server.dispatch_lock.close();self.server.job_lock.release()
    def test_confirmed_clear_reconciles_only_own_old_epoch_requests(self):
        ledger=self.server.ledger;goal=Goal('BUILD_A');ledger.accept(goal.meta,'arm2')
        other=Goal('BUILD_A');other.meta['client_id']='d'*32;ledger.accept(other.meta,'arm2')
        after=RequestLedger(self.f.module.ROOT,'after-restart');current=Goal('BUILD_B');after.accept(current.meta,'arm2')
        self.assertEqual(after.clear_abandoned(goal.meta['client_id']),1)
        self.assertEqual(after.response(goal.meta,'arm2'),(False,'CLEARED'))
        self.assertEqual(after.response(other.meta,'arm2'),(False,'UNKNOWN'))
        self.assertEqual(after.response(current.meta,'arm2'),(False,'PENDING'))
    def test_conflicting_identity_rejected(self):
        goal=Goal('BUILD_A')
        with patch.object(self.f.module.subprocess,'Popen',return_value=Process(['A_DONE'])):self.f.execute('arm2',goal)
        other=self.retry(goal);other.meta['command']='BUILD_B';other.request.command=json.dumps(other.meta)
        self.assertFalse(self.server.goal_callback(other.request))
    def test_expired_or_raw_motion_requests_cannot_start(self):
        self.server.health.accepts=lambda meta:False
        self.assertFalse(self.server.goal_callback(Goal('BUILD_A').request))
        self.assertFalse(self.server.goal_callback(SimpleNamespace(command='BUILD_A')))
        self.assertTrue(self.server.goal_callback(SimpleNamespace(command='EMER_STOP')))
    def test_emergency_query_is_read_only(self):
        ticket=self.server.gate.reserve();goal=Goal('EMER_STOP');query=self.retry(goal,'query')
        self.assertEqual(self.f.execute('arm2',query).message,'NOT_FOUND')
        self.assertFalse(ticket.stopped)
    def test_missing_admission_is_never_executed(self):
        with patch.object(self.f.module.subprocess,'Popen') as start:
            with self.assertRaisesRegex(RuntimeError,'MISSING_ADMISSION'):self.server.execute_callback(Goal('BUILD_A'))
            start.assert_not_called()

class StandbyServiceEnvironmentTests(unittest.TestCase):
    def test_health_publication_works_with_only_integration_on_service_import_path(self):
        # The ROS service runs action_server.py directly with system Python.
        # Desktop tests add the project root, which used to hide this failure.
        code=r"""
import json,sys,threading,tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from supervision_session import SupervisionHealth
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder)
    h=SupervisionHealth.__new__(SupervisionHealth)
    h.node=SimpleNamespace(runtime_root=root)
    h.gate=SimpleNamespace(lock=threading.RLock(),generation=4)
    h.lock=threading.RLock();h.epoch='service-epoch';h.seen={'c'*32:100.}
    h.publish_standby_health()
    path=root/'data/standby-supervision.json'
    assert json.loads(path.read_text())=={'epoch':'service-epoch','generation':4,'seen':{'c'*32:100.}}
    h.gate.generation=5;h.seen={'c'*32:101.};h.publish_standby_health()
    value=json.loads(path.read_text())
    assert value['generation']==5 and value['seen']['c'*32]==101.
    assert not list(path.parent.glob('.ledger-*'))
"""
        result=subprocess.run([sys.executable,'-I','-S','-c',code,str(Path(__file__).resolve().parents[1]/'integration')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


class LeaseTests(unittest.TestCase):
    def health(self):
        h=object.__new__(SupervisionHealth);h.lock=threading.RLock();h.epoch='epoch';h.seen={};h.challenges={};h.fences={}
        h.publisher=Mock();h.String=SimpleNamespace;h.notifications=queue.Queue(maxsize=64);h.gate=SupervisionGate();h.notice=Mock();h.stop=threading.Event()
        self.addCleanup(lambda:h.gate.retire(h.gate.active));return h
    def test_old_heartbeat_and_stale_goal_do_not_renew_lease(self):
        h=self.health();h.challenges={'old':time.monotonic()-2};client='c'*32
        h.receive(SimpleNamespace(data=json.dumps(dict(client_id=client,epoch='epoch',nonce='old'))))
        self.assertFalse(h.fresh(client));h.challenges['now']=time.monotonic();h.fences['now']=(h.gate.generation,None,None)
        h.receive(SimpleNamespace(data=json.dumps(dict(client_id=client,epoch='epoch',nonce='now'))))
        self.assertTrue(h.fresh(client));self.assertTrue(h.accepts(dict(client_id=client,epoch='epoch',nonce='now')))
        self.assertFalse(h.accepts(dict(client_id=client,epoch='older',nonce='now')))
    def test_standby_file_publication_never_runs_on_the_stop_watcher(self):
        h=self.health();ticket=h.gate.reserve(client_id='c'*32)
        h.publish_standby_health=Mock(side_effect=lambda:time.sleep(5))
        worker=threading.Thread(target=h.watch);worker.start()
        try:
            deadline=time.monotonic()+1
            while not ticket.stopped and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(ticket.stopped);h.publish_standby_health.assert_not_called()
        finally:h.stop.set();worker.join(1)
    def test_loss_signals_hold_once_and_reconnect_does_not_resume(self):
        h=self.health();ticket=h.gate.reserve(client_id='c'*32);worker=threading.Thread(target=h.watch);worker.start()
        try:
            deadline=time.monotonic()+1
            while not ticket.link_lost and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(ticket.link_lost);self.assertTrue(ticket.stopped);self.assertEqual(os.read(ticket.read_fd,1),b'!')
            h.seen[ticket.client_id]=time.monotonic();time.sleep(.1)
            self.assertTrue(ticket.stopped);self.assertEqual(h.notifications.get_nowait(),ticket.job_id);h.notice.assert_not_called()
        finally:h.stop.set();worker.join(1)

class ClientBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):PCFixture.setUpClass()
    def setUp(self):
        self.f=PCFixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.client=self.f.client()
    def test_timeout_completes_future_unknown_preserves_business_id(self):
        future=self.client.send_goal('BUILD_A');key=self.client.journal.value['job']['request_id']
        self.client._requests[0]['expires']=0;self.client.monitor_jobs=False;self.client.poll()
        self.assertEqual(future.result().message,'UNKNOWN');self.assertTrue(self.client.busy)
        self.assertEqual(self.client.journal.value['job']['request_id'],key)
        self.assertFalse(self.client._requests)
    def test_disconnect_does_not_report_success_or_clear_busy(self):
        future=self.client.send_goal('BUILD_A');self.client.health.online=lambda:False;self.client.poll()
        self.assertEqual(future.result().message,'UNKNOWN');self.assertTrue(self.client.busy)
        self.assertEqual(self.client.communication,'COMM_LOST')
    def test_restart_uses_persisted_id_and_query_only(self):
        self.client.send_goal('BUILD_A');key=self.client.journal.value['job']['request_id'];identity=self.client.journal.value['client_id']
        self.client.destroy_node();other=self.f.client();other._next_query=0;other.poll()
        self.assertTrue(other.busy);self.assertEqual(other.journal.value['client_id'],identity)
        self.assertEqual(other._requests[0]['operation'],'query');self.assertEqual(other._requests[0]['request_id'],key)
    def test_duplicate_process_cannot_own_same_identity(self):
        with self.assertRaisesRegex(RuntimeError,'다른 관제'):self.f.client()

    def test_repeating_uncertain_reset_queries_original_id(self):
        future=self.client.send_goal('RESET');key=self.client.journal.value['control']['request_id']
        self.client._requests[0]['expires']=0;self.client.poll();self.assertEqual(future.result().message,'UNKNOWN')
        self.client.send_goal('RESET');request=self.client._requests[-1]
        self.assertEqual(request['operation'],'query');self.assertEqual(request['request_id'],key)


class ParentLossTests(unittest.TestCase):
    def test_dead_status_reader_keeps_hold_and_local_recording(self):
        from tests.test_episode_cli import cli
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)):
            (Path(folder)/'data').mkdir()
            status=cli.Status('arm2','A',False);control=cli.Control();status.control=control
            try:
                with patch('builtins.print',side_effect=BrokenPipeError):status.emit('PAUSED')
                self.assertTrue(control.pause.is_set());self.assertTrue(status.stdout_lost)
                status.emit('PAUSED');status.flush_records()
                self.assertEqual(len(json.loads(status.log.read_text())['events']),2)
            finally:status.close()
