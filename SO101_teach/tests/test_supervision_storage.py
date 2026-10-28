"""Persistent-write failures and end-to-end stop contexts; no hardware or ROS needed."""
from collections import OrderedDict
from concurrent.futures import Future
import errno,json,os,queue,stat,tempfile,threading,time,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
from tests.test_action_contract import Goal,ActionContractTests as ServerFixture,Process
from tests.test_pc_action_client import PCActionClientTests as PCFixture
from tests.test_episode_cli import cli
from supervision_gate import SupervisionGate
from supervision_session import SupervisionHealth,RequestLedger
import supervision_session

class StopContextTests(unittest.TestCase):
    def test_pc_nonce_is_bound_to_global_and_local_stop_state(self):
        h=object.__new__(SupervisionHealth);h.gate=SupervisionGate();h.lock=threading.RLock();h.challenges=OrderedDict();h.fences={};h.seen={};h.epoch='test'
        h.notifications=queue.Queue();h.notice=Mock();h.String=SimpleNamespace;h.publisher=Mock()
        state={'job_id':'a'*32,'stop_generation':0}
        h.node=SimpleNamespace(runtime_root=Path('/unused'),protocol=SimpleNamespace(control_state=lambda *a,**k:dict(state)))
        def issue():
            h.challenge();nonce=json.loads(h.publisher.publish.call_args.args[0].data)['nonce']
            meta=dict(client_id='c'*32,epoch='test',nonce=nonce)
            h.receive(SimpleNamespace(data=json.dumps(meta)));return meta
        old=issue();self.assertTrue(h.accepts(old));self.assertTrue(h.control_fence(old,state))
        h.gate.stop_active();self.assertFalse(h.accepts(old))
        current=issue();self.assertTrue(h.accepts(current))
        state['stop_generation']=1
        self.assertFalse(h.control_fence(current,state))
        self.assertTrue(h.control_fence(issue(),state))

class ReceiptStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        p=patch.object(cli,'ROOT',Path(self.temp.name));p.start();self.addCleanup(p.stop)
    def test_failed_write_never_enables_restart_or_reset_and_retry_cannot_reexecute(self):
        for command in ('restart','reset'):
            c=cli.Control();c.paused=True;c.pause.set()
            value=dict(job_id=c.job_id,request_id='a'*32,command=command,stop_generation=0)
            with patch.object(cli,'atomic_json',side_effect=OSError(errno.ENOSPC,'full')):
                with self.assertRaises(OSError):c.command('control '+json.dumps(value))
            self.assertFalse(c.resume.is_set());self.assertFalse(c.reset.is_set());self.assertTrue(c.pause.is_set())
            self.assertEqual(json.loads(c.command('control '+json.dumps(value)))['state'],'failed')
            self.assertFalse(c.resume.is_set());self.assertFalse(c.reset.is_set())
    def test_stop_arriving_during_receipt_write_is_consumed_before_motion_permission(self):
        for command in ('restart','reset'):
            c=cli.Control();c.paused=True;c.pause.set();gate=SupervisionGate();ticket=gate.reserve();done=threading.Event()
            worker=c.configure_supervision(ticket.job_id,os.dup(ticket.read_fd),done)
            original=c.save_receipts;stopped=False
            def write():
                nonlocal stopped
                if not stopped:stopped=True;gate.stop_active()
                original()
            try:
                with patch.object(c,'save_receipts',side_effect=write),self.assertRaisesRegex(ValueError,'STALE_CONTROL'):
                    c.command('control '+json.dumps(dict(job_id=c.job_id,request_id='a'*32,command=command,stop_generation=0)))
                self.assertFalse(c.resume.is_set());self.assertFalse(c.reset.is_set())
            finally:done.set();gate.retire(ticket);worker.join(1)
    def test_failed_completion_write_does_not_publish_completed_receipt(self):
        c=cli.Control();c.receipts['a'*32]={'command':'restart','state':'pending'}
        with patch.object(cli,'atomic_json',side_effect=OSError(errno.EIO,'failure')):
            with self.assertRaises(OSError):c.complete('restart')
        self.assertEqual(c.receipts['a'*32]['state'],'pending')

class ResultRepairTests(unittest.TestCase):
    def test_result_repairs_in_same_epoch_without_reexecution(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=RequestLedger(root,'epoch');meta=Goal('BUILD_A').meta;ledger.accept(meta,'arm2')
            with patch.object(supervision_session,'atomic_record',side_effect=OSError(errno.EIO,'temporary')):
                with self.assertRaises(OSError):ledger.finish(meta,'arm2',True,'IDLE')
                self.assertEqual(ledger.response(meta,'arm2'),(False,'STORAGE_PENDING'))
            self.assertEqual(ledger.response(meta,'arm2'),(True,'IDLE'));self.assertFalse(ledger.pending_results)
            self.assertEqual(RequestLedger(root,'restarted').response(meta,'arm2'),(True,'IDLE'))
    def test_clear_repairs_known_result_but_does_not_clear_live_same_epoch_job(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=RequestLedger(root,'epoch');meta=Goal('BUILD_A').meta;live=Goal('BUILD_B').meta
            ledger.accept(meta,'arm2');ledger.accept(live,'arm2')
            with patch.object(supervision_session,'atomic_record',side_effect=OSError(errno.EIO,'temporary')):
                with self.assertRaises(OSError):ledger.finish(meta,'arm2',False,'ERROR')
            self.assertEqual(ledger.clear_abandoned(meta['client_id']),1)
            self.assertEqual(ledger.response(meta,'arm2'),(False,'ERROR'));self.assertEqual(ledger.response(live,'arm2'),(False,'PENDING'))
    def test_restart_after_unsaved_result_keeps_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=RequestLedger(root,'epoch');meta=Goal('BUILD_A').meta;ledger.accept(meta,'arm2')
            with patch.object(supervision_session,'atomic_record',side_effect=OSError(errno.EIO,'temporary')):
                with self.assertRaises(OSError):ledger.finish(meta,'arm2',True,'IDLE')
            restarted=RequestLedger(root,'new');self.assertEqual(restarted.response(meta,'arm2'),(False,'UNKNOWN'))

class PCStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):PCFixture.setUpClass()
    def setUp(self):
        self.f=PCFixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.c=self.f.client()
    def test_emergency_sends_before_failed_save_and_future_still_completes(self):
        def fail():
            self.assertEqual(self.c._client.send_goal_async.call_count,1)
            raise OSError(errno.ENOSPC,'full')
        with patch.object(self.c.journal,'save',side_effect=fail):
            future=self.c.send_goal('EMER_STOP');request=self.c._requests[-1]
            self.c._finish(request,True,'IDLE','confirmed')
        self.assertTrue(future.result().success);self.assertIsNotNone(self.c.storage_error)
        self.c._next_save=0;self.c.poll();self.assertIsNone(self.c.storage_error)
    def test_failed_ordinary_save_does_not_dispatch_or_replace_previous_record(self):
        before=self.c.journal.value['job']
        with patch.object(self.c.journal,'save',side_effect=OSError(errno.EIO,'disk')):
            result=self.c.send_goal('BUILD_A').result()
        self.assertFalse(result.success);self.c._client.send_goal_async.assert_not_called();self.assertIs(self.c.journal.value['job'],before)
    def test_storage_pending_result_preserves_automatic_same_id_query(self):
        self.c.send_goal('BUILD_A');request=self.c._requests[-1];request['operation']='query'
        future=Future();future.set_result(SimpleNamespace(status=4,result=SimpleNamespace(success=False,message='STORAGE_PENDING')))
        self.c.get_result_callback(future,request=request)
        self.assertTrue(self.c.busy);self.assertTrue(self.c.journal.value['job']['auto_query'])
        self.assertEqual(self.c.last_result.message,'UNKNOWN')


class StorageBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ServerFixture.setUpClass()
    def setUp(self):
        self.f=ServerFixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.s=self.f.server
    def replay(self,goal):
        query=Goal(goal.meta['command']);query.meta={**goal.meta,'attempt_id':uuid.uuid4().hex,'operation':'query'}
        query.request.command=json.dumps(query.meta)
        self.assertTrue(self.s.goal_callback(query.request))
        return self.s.execute_callback(query)
    def test_admission_fsync_failure_after_rename_recovers_without_execution(self):
        goal=Goal('BUILD_A');real_fsync=os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EIO,'directory fsync')
            real_fsync(fd)
        with patch.object(self.s,'notice'),patch.object(supervision_session.os,'fsync',side_effect=fail_directory),patch.object(self.f.module.subprocess,'Popen') as start:
            self.assertFalse(self.s.goal_callback(goal.request))
            self.assertEqual(self.s.ledger.lookup(goal.meta,'arm2')['state'],'accepted')
            self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
            start.assert_not_called()
        self.assertIsNone(self.s.gate.active);self.assertFalse(self.s.job_lock.locked())
        self.assertEqual(self.replay(goal).message,'ERROR')
        self.assertEqual(RequestLedger(self.f.temp.name,'new').response(goal.meta,'arm2'),(False,'ERROR'))
        with patch.object(self.f.module.subprocess,'Popen') as start:
            duplicate=Goal('BUILD_A');duplicate.meta={**goal.meta,'attempt_id':uuid.uuid4().hex};duplicate.request.command=json.dumps(duplicate.meta)
            self.assertTrue(self.s.goal_callback(duplicate.request));self.assertEqual(self.s.execute_callback(duplicate).message,'ERROR');start.assert_not_called()
    def test_admission_failure_before_file_creation_also_fences_duplicates(self):
        goal=Goal('BUILD_A')
        with patch.object(supervision_session,'atomic_record',side_effect=OSError(errno.ENOSPC,'full')):
            self.assertFalse(self.s.goal_callback(goal.request))
            self.assertFalse(self.s.ledger.path(goal.meta).exists())
            self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
        self.assertEqual(self.replay(goal).message,'ERROR')
        self.assertIsNone(self.s.gate.active)
    def test_read_error_during_admission_or_replay_keeps_verified_result_recoverable(self):
        goal=Goal('BUILD_A');self.s.ledger.accept(goal.meta,'arm2')
        with patch.object(supervision_session,'atomic_record',side_effect=OSError(errno.EIO,'write')):
            with self.assertRaises(OSError):self.s.ledger.finish(goal.meta,'arm2',True,'IDLE')
        real_read=Path.read_text
        def fail_read(path,*a,**kw):
            if path==self.s.ledger.path(goal.meta):raise OSError(errno.EIO,'read')
            return real_read(path,*a,**kw)
        with patch.object(Path,'read_text',fail_read):self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
        self.assertEqual(self.replay(goal).message,'IDLE')
    def test_read_error_after_goal_acceptance_does_not_escape_action_callback(self):
        goal=Goal('BUILD_A');self.s.ledger.accept(goal.meta,'arm2');self.s.ledger.finish(goal.meta,'arm2',True,'IDLE')
        goal.meta['operation']='query';goal.request.command=json.dumps(goal.meta)
        self.assertTrue(self.s.goal_callback(goal.request))
        with patch.object(self.s.ledger,'lookup',side_effect=OSError(errno.EIO,'read')):
            self.assertEqual(self.s.execute_callback(goal).message,'STORAGE_PENDING')
        self.assertEqual(self.replay(goal).message,'IDLE')

class InvalidResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):PCFixture.setUpClass()
    def test_empty_or_inconsistent_results_keep_job_busy_and_same_id_query(self):
        for status,success,message in ((6,False,''),(4,True,'ERROR'),(6,False,'IDLE'),(5,False,'ERROR')):
            with self.subTest(status=status,success=success,message=message):
                f=PCFixture();f.setUp()
                try:
                    client=f.client();client.send_goal('BUILD_A');request=client._requests[-1]
                    reply=Future();reply.set_result(SimpleNamespace(status=status,result=SimpleNamespace(success=success,message=message)))
                    client.get_result_callback(reply,request=request)
                    self.assertTrue(client.busy);self.assertEqual(client.last_result.message,'UNKNOWN')
                    self.assertTrue(client.journal.value['job']['auto_query'])
                    key=request['request_id'];client._next_query=0;client.poll()
                    self.assertEqual(client._requests[-1]['operation'],'query');self.assertEqual(client._requests[-1]['request_id'],key)
                finally:f.doCleanups()
