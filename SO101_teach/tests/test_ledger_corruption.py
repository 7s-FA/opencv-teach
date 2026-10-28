"""Malformed durable results cannot become success or disable same-ID recovery."""
import json,unittest,uuid
from unittest.mock import patch
from tests.test_supervision_storage import StorageBoundaryTests
from tests.test_action_contract import Goal
from supervision_session import RequestLedger

class LedgerCorruptionTests(unittest.TestCase):
    setUpClass=classmethod(lambda cls:StorageBoundaryTests.setUpClass())
    setUp=StorageBoundaryTests.setUp
    replay=StorageBoundaryTests.replay
    def test_invalid_records_remain_storage_pending_then_recover_without_execution(self):
        goal=Goal('BUILD_A');ledger=self.s.ledger;ledger.accept(goal.meta,'arm2');ledger.finish(goal.meta,'arm2',True,'IDLE')
        path=ledger.path(goal.meta);original=path.read_bytes();valid=json.loads(original)
        invalid=[b'\xff',b'null',b'[]',json.dumps({**valid,'success':'false'}).encode(),json.dumps({**valid,'success':1}).encode(),
                 json.dumps({k:v for k,v in valid.items() if k!='state'}).encode(),json.dumps({**valid,'state':'completed','success':False}).encode(),
                 json.dumps({**valid,'state':'accepted','success':True}).encode()]
        with patch.object(self.f.module.subprocess,'Popen') as start:
            for raw in invalid:
                with self.subTest(record=raw[:80]):
                    path.write_bytes(raw)
                    self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
                    duplicate=Goal('BUILD_A');duplicate.meta={**goal.meta,'attempt_id':uuid.uuid4().hex};duplicate.request.command=json.dumps(duplicate.meta)
                    self.assertTrue(self.s.goal_callback(duplicate.request));self.assertEqual(self.s.execute_callback(duplicate).message,'STORAGE_PENDING')
                    path.write_bytes(original);self.assertEqual(self.replay(goal).message,'IDLE')
            start.assert_not_called()
        self.assertEqual(RequestLedger(self.f.temp.name,'restarted').response(goal.meta,'arm2'),(True,'IDLE'))
    def test_valid_conflicting_identity_remains_rejected(self):
        goal=Goal('BUILD_A');self.s.ledger.accept(goal.meta,'arm2')
        other=Goal('BUILD_B');other.meta['request_id']=goal.meta['request_id'];other.request.command=json.dumps(other.meta)
        self.assertFalse(self.s.goal_callback(other.request))
