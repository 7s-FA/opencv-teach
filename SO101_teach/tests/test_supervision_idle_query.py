"""Idle context and query tombstone fault boundaries, without devices or ROS."""
from collections import OrderedDict
from concurrent.futures import Future
import errno,json,queue,threading,uuid
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
from tests.test_action_contract import ActionContractTests as ServerFixture,Goal
from tests.test_pc_action_client import PCActionClientTests as PCFixture
from supervision_session import SupervisionHealth
import supervision_session

class IdleContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ServerFixture.setUpClass()
    def setUp(self):
        self.f=ServerFixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.s=self.f.server
        h=object.__new__(SupervisionHealth);h.gate=self.s.gate;h.lock=threading.RLock()
        h.challenges=OrderedDict();h.fences={};h.seen={};h.epoch='test';h.notifications=queue.Queue()
        h.notice=Mock();h.String=SimpleNamespace;h.publisher=Mock()
        h.node=SimpleNamespace(runtime_root=self.f.module.ROOT,protocol=SimpleNamespace(control_state=Mock(side_effect=FileNotFoundError)))
        self.h=h;self.s.health=h
    def goal(self,command='RESET'):
        self.h.challenge();nonce=json.loads(self.h.publisher.publish.call_args.args[0].data)['nonce']
        g=Goal(command);g.meta.update(epoch=self.h.epoch,nonce=nonce);g.request.command=json.dumps(g.meta)
        self.h.receive(SimpleNamespace(data=json.dumps(g.meta)));return g
    def test_old_job_reset_cannot_become_idle_reset_but_fresh_idle_reset_works(self):
        ticket=self.s.gate.reserve('a'*32,'c'*32)
        self.h.node.protocol.control_state.side_effect=None
        self.h.node.protocol.control_state.return_value={'job_id':ticket.job_id,'stop_generation':1}
        delayed=self.goal();self.s.gate.retire(ticket)
        self.h.node.protocol.control_state.side_effect=FileNotFoundError
        with patch.object(self.s.protocol,'control_state',side_effect=FileNotFoundError),patch.object(self.f.module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='A_RESET_DONE\n',stderr='')) as launch:
            self.assertTrue(self.s.goal_callback(delayed.request));self.assertFalse(self.s.execute_callback(delayed).success);launch.assert_not_called()
            current=self.goal();self.assertTrue(self.s.goal_callback(current.request));self.assertTrue(self.s.execute_callback(current).success)
            self.assertEqual(launch.call_count,1)
    def test_executor_context_is_checked_even_without_a_gate_generation_change(self):
        self.h.node.protocol.control_state.side_effect=None
        self.h.node.protocol.control_state.return_value={'job_id':'a'*32,'stop_generation':1}
        delayed=self.goal()
        with patch.object(self.s.protocol,'control_state',side_effect=FileNotFoundError),patch.object(self.f.module.subprocess,'run') as launch:
            self.assertTrue(self.s.goal_callback(delayed.request));self.assertFalse(self.s.execute_callback(delayed).success);launch.assert_not_called()
    def test_probe_errors_and_missing_endpoint_with_reservation_are_not_idle(self):
        for failure in (TimeoutError(),OSError(errno.EIO,'read'),ValueError('bad state')):
            self.h.node.protocol.control_state.side_effect=failure
            self.assertFalse(self.h.control_fence(self.goal().meta,{}))
        self.h.node.protocol.control_state.side_effect=FileNotFoundError
        ticket=self.s.gate.reserve();self.assertFalse(self.h.control_fence(self.goal().meta,{}))
        self.s.gate.retire(ticket);self.assertTrue(self.h.control_fence(self.goal().meta,{}))
    def test_old_idle_nonce_is_invalid_after_an_intervening_job_finishes(self):
        old=self.goal();ticket=self.s.gate.reserve();self.s.gate.retire(ticket)
        self.assertFalse(self.h.control_fence(old.meta,{}))
        self.assertTrue(self.h.accepts(old.meta))  # New jobs need not wait for a new heartbeat.
        generation=self.s.gate.context_generation;self.s.gate.retire(ticket)
        self.assertEqual(generation,self.s.gate.context_generation)

class FirstQueryStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):ServerFixture.setUpClass();PCFixture.setUpClass()
    def test_storage_failure_on_missing_request_keeps_auto_query_and_fences_late_execute(self):
        for phase in ('accept','finish'):
            with self.subTest(phase=phase):
                f=ServerFixture();f.setUp();p=PCFixture();p.setUp();c=p.client();s=f.server
                try:
                    key=uuid.uuid4().hex;c.send_goal('BUILD_A',request_id=key)
                    c._finish(c._requests[-1],False,'UNKNOWN','lost before admission',definitive=False)
                    c.retry_last();request=c._requests[-1]
                    query=Goal('BUILD_A');query.meta.update(request_id=key,operation='query');query.request.command=json.dumps(query.meta)
                    real=supervision_session.atomic_record
                    def failing(path,value):
                        if phase=='accept' or value.get('state')!='accepted':raise OSError(errno.ENOSPC,'full')
                        return real(path,value)
                    with patch.object(supervision_session,'atomic_record',side_effect=failing):
                        self.assertTrue(s.goal_callback(query.request));result=s.execute_callback(query)
                        self.assertEqual(result.message,'STORAGE_PENDING')
                        reply=Future();reply.set_result(SimpleNamespace(status=4,result=result));c.get_result_callback(reply,request=request)
                    self.assertTrue(c.busy);self.assertTrue(c.journal.value['job']['auto_query'])
                    c._next_query=0;c.poll();request=c._requests[-1];self.assertEqual(request['request_id'],key);self.assertEqual(request['operation'],'query')
                    query.meta['attempt_id']=uuid.uuid4().hex;query.request.command=json.dumps(query.meta)
                    self.assertTrue(s.goal_callback(query.request));result=s.execute_callback(query)
                    self.assertIn(result.message,('ERROR','NOT_FOUND'))
                    reply=Future();reply.set_result(SimpleNamespace(status=4,result=result));c.get_result_callback(reply,request=request)
                    self.assertFalse(c.busy)
                    query.meta.update(operation='execute',attempt_id=uuid.uuid4().hex);query.request.command=json.dumps(query.meta)
                    with patch.object(f.module.subprocess,'Popen') as launch:
                        self.assertTrue(s.goal_callback(query.request));self.assertEqual(s.execute_callback(query).message,result.message);launch.assert_not_called()
                finally:p.doCleanups();f.doCleanups()
