"""Disk/transport fault boundaries use temporary files and fake executors only."""
from concurrent.futures import Future
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from tests.test_action_contract import ActionContractTests as ServerFixture, Goal, Process
from tests.test_pc_action_client import PCActionClientTests as ClientFixture
from tests.test_wpf_bridge import BridgeTests as BridgeFixture, BridgeService
from supervision_session import RequestLedger
import supervision_session


@contextmanager
def fail_directory_sync_after_replace(path, observed, *, state=None):
    original = os.fsync
    def sync(fd):
        if (stat.S_ISDIR(os.fstat(fd).st_mode)
                and Path(os.readlink(f'/proc/self/fd/{fd}')) == path.parent
                and path.exists()):
            value = json.loads(path.read_text())
            if state is None or value.get('state') == state:
                observed.append(value)
                raise OSError(errno.EIO, 'injected directory fsync after replace')
        return original(fd)
    with patch.object(os, 'fsync', side_effect=sync):
        yield


class ResultRecoveryBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ServerFixture.setUpClass()
        ClientFixture.setUpClass()

    def setUp(self):
        self.fixture = ServerFixture(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = self.fixture.server

    def duplicate(self, original, operation='query'):
        goal = Goal(original.meta['command'])
        goal.meta = {**original.meta, 'attempt_id': uuid.uuid4().hex, 'operation': operation}
        goal.request.command = json.dumps(goal.meta)
        return goal

    def result(self, goal):
        self.assertTrue(self.server.goal_callback(goal.request))
        return self.server.execute_callback(goal)

    def test_one_read_failure_during_query_admission_keeps_automatic_requery(self):
        original = Goal('BUILD_A'); query = self.duplicate(original)
        with patch.object(self.server.ledger, 'lookup', side_effect=OSError(errno.EIO, 'read')):
            self.assertTrue(self.server.goal_callback(query.request))
        result = self.server.execute_callback(query)
        self.assertEqual(result.message, 'STORAGE_PENDING')
        fixture = ClientFixture(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        client = fixture.client()
        client.send_goal('BUILD_A', request_id=original.meta['request_id'])
        request = client._requests[-1]; request['operation'] = 'query'
        reply = Future(); reply.set_result(SimpleNamespace(status=4, result=result))
        client.get_result_callback(reply, request=request)
        self.assertTrue(client.busy)
        self.assertTrue(client.journal.value['job']['auto_query'])
        client._next_query = 0; client.poll()
        self.assertEqual(client._requests[-1]['operation'], 'query')
        self.assertEqual(client._requests[-1]['request_id'], original.meta['request_id'])
        with patch.object(self.fixture.module.subprocess, 'Popen') as start:
            self.assertEqual(self.result(self.duplicate(original)).message, 'NOT_FOUND')
            self.assertEqual(self.result(self.duplicate(original, 'execute')).message, 'NOT_FOUND')
            start.assert_not_called()

    def test_storage_retry_does_not_claim_old_epoch_unknown_job_completed(self):
        original = Goal('BUILD_A')
        self.server.ledger.accept(original.meta, 'arm2')
        self.server.ledger = RequestLedger(self.fixture.temp.name, 'new-epoch')
        query = self.duplicate(original)
        with patch.object(self.server.ledger, 'lookup', side_effect=OSError(errno.EIO, 'read')):
            self.assertTrue(self.server.goal_callback(query.request))
        self.assertEqual(self.server.execute_callback(query).message, 'STORAGE_PENDING')
        with patch.object(self.fixture.module.subprocess, 'Popen') as start:
            self.assertEqual(self.result(self.duplicate(original)).message, 'UNKNOWN')
            self.assertEqual(self.result(self.duplicate(original, 'execute')).message, 'UNKNOWN')
            start.assert_not_called()

    def test_completion_directory_fsync_and_replay_read_failure_repair_without_rerun(self):
        goal = Goal('BUILD_A'); path = self.server.ledger.path(goal.meta); observed = []
        with patch.object(self.fixture.module.subprocess, 'Popen', return_value=Process(['A_DONE'])) as start:
            with fail_directory_sync_after_replace(path, observed, state='completed'):
                self.assertEqual(self.result(goal).message, 'UNKNOWN')
                self.assertEqual(self.result(self.duplicate(goal)).message, 'STORAGE_PENDING')
                real_read = Path.read_text
                def read(file, *args, **kwargs):
                    if file == path: raise OSError(errno.EIO, 'replay read')
                    return real_read(file, *args, **kwargs)
                with patch.object(Path, 'read_text', read):
                    self.assertEqual(self.result(self.duplicate(goal)).message, 'STORAGE_PENDING')
            self.assertTrue(observed)
            self.assertTrue(all(row['success'] and row['state'] == 'completed' for row in observed))
            self.assertEqual(self.result(self.duplicate(goal)).message, 'IDLE')
            self.assertFalse(self.server.ledger.pending_results)
            self.server.ledger = RequestLedger(self.fixture.temp.name, 'restarted')
            for operation in ('query', 'execute', 'execute'):
                self.assertEqual(self.result(self.duplicate(goal, operation)).message, 'IDLE')
            self.assertEqual(start.call_count, 1)

    def test_failed_admission_survives_restart_as_unknown_without_execution(self):
        goal = Goal('BUILD_A'); path = self.server.ledger.path(goal.meta); observed = []
        with patch.object(self.fixture.module.subprocess, 'Popen') as start:
            with fail_directory_sync_after_replace(path, observed, state='accepted'):
                self.assertFalse(self.server.goal_callback(goal.request))
            self.assertTrue(observed)
            self.server.ledger = RequestLedger(self.fixture.temp.name, 'restarted')
            self.assertEqual(self.result(self.duplicate(goal)).message, 'UNKNOWN')
            self.assertEqual(self.result(self.duplicate(goal, 'execute')).message, 'UNKNOWN')
            start.assert_not_called()


class BridgeRecoveryBoundaries(unittest.TestCase):
    setUp = BridgeFixture.setUp
    connect = BridgeFixture.connect
    request = BridgeFixture.request

    def test_cancelled_or_empty_query_result_does_not_escape_callback(self):
        for index, empty in enumerate((False, True)):
            key = str(index + 1) * 32
            self.service.records[key] = self.service.make_record(key, 'BUILD_A', 'arm2', 'UNKNOWN', 'waiting')
            future = Future(); self.arms['arm2'].query_request = lambda *a, **kw: future
            self.service.query(self.generation, key)
            with patch('concurrent.futures._base.LOGGER.exception') as logged:
                if empty: future.set_result(None)
                else: future.cancel()
                logged.assert_not_called()
            self.assertEqual(self.service.records[key]['state'], 'UNKNOWN')
            self.assertEqual(self.service.records[key]['detail'], '전송 결과 미확인')
            self.assertFalse(self.arms['arm2'].sent)

    def test_query_future_error_stays_recoverable_and_next_query_never_executes(self):
        key = 'a' * 32
        record = self.service.make_record(key, 'BUILD_A', 'arm2', 'UNKNOWN', 'pending query')
        self.service.records[key] = record
        future = Future(); self.arms['arm2'].query_request = lambda *a, **kw: future
        self.service.query(self.generation, key)
        with patch('concurrent.futures._base.LOGGER.exception') as logged:
            future.set_exception(OSError(errno.EIO, 'result read'))
            logged.assert_not_called()
        self.assertEqual(record['state'], 'UNKNOWN')
        self.assertEqual(record['detail'], '전송 결과 미확인')
        recovered = Future()
        recovered.set_result(SimpleNamespace(success=True, message='IDLE', detail='verified', accepted=True))
        self.arms['arm2'].query_request = lambda *a, **kw: recovered
        self.service.query(self.generation, key)
        self.assertEqual(record['state'], 'SUCCEEDED')
        self.assertEqual(self.service.submit(self.request(key=key))['code'], 'EXISTING_REQUEST')
        self.assertFalse(self.arms['arm2'].sent)

    def test_receipt_directory_fsync_failure_repairs_without_deferred_dispatch(self):
        key = 'b' * 32; path = self.service.root / (key + '.json'); observed = []
        with fail_directory_sync_after_replace(path, observed):
            result = self.service.submit(self.request(key=key))
            self.assertEqual(result['code'], 'STORAGE_ERROR')
        self.assertTrue(observed); self.assertIn(key, self.service.dirty)
        self.assertFalse(self.arms['arm2'].sent)
        self.service.maintenance(); self.assertFalse(self.service.dirty)
        self.assertFalse(self.arms['arm2'].sent)
        self.service.close(self.generation)
        self.service = BridgeService(self.arms, self.temp.name, clock=lambda: self.now)
        self.connect()
        self.assertEqual(self.service.submit(self.request(key=key))['code'], 'EXISTING_REQUEST')
        recovered = Future()
        recovered.set_result(SimpleNamespace(success=False, message='NOT_FOUND', detail='fenced', accepted=True))
        self.arms['arm2'].query_request = lambda *a, **kw: recovered
        self.service.query(self.generation, key)
        self.assertEqual(self.service.records[key]['state'], 'FAILED')
        self.assertFalse(self.arms['arm2'].sent)
