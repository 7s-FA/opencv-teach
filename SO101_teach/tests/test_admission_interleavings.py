"""Adversarial admission/query schedules; all transports and devices are fakes."""
from tests.test_wpf_bridge import BridgeTests, AsyncClientStorageTests

class ReceiptOrderingTests(BridgeTests):
    # Reuse setup without inheriting the existing suite.
    def test_query_waits_for_receipt_admission(self):
        writes=[];self.service.writer=lambda path,value,done:writes.append(done)
        self.service.submit(self.request())
        self.service.query(self.generation,'1'*32)
        self.assertFalse(self.arms['arm2'].queries)
        writes[0](None)
        self.assertEqual(self.arms['arm2'].sent,[('BUILD_A','1'*32)])

    def test_pending_control_reserves_admission_before_journal_exists(self):
        self.service.writer=lambda *args:None
        self.service.submit(self.request('RESTART'))
        second=self.service.submit(self.request('RESET','2'*32))
        self.assertEqual(second['code'],'NOT_ALLOWED')
        self.assertEqual(self.service.submit(self.request('BUILD_A','3'*32))['code'],'NOT_ALLOWED')
        self.assertTrue(self.service.submit(self.request('EMER_STOP','4'*32))['accepted'])
        self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','4'*32)])

class JournalOrderingTests(AsyncClientStorageTests):
    def test_manual_query_before_journal_durability_shares_execution_future(self):
        future=self.client.send_goal('BUILD_A',request_id='a'*32)
        queried=self.client.query_request('BUILD_A','a'*32)
        self.assertIs(queried,future)
        self.assertFalse(self.client._client.send_goal_async.called)
        self.writes[0][1](None)
        self.assertEqual(self.client._client.send_goal_async.call_count,1)

    def test_automatic_poll_does_not_query_unsent_journal(self):
        self.client.send_goal('BUILD_A',request_id='a'*32)
        self.client._next_query=0
        self.client.poll()
        self.assertFalse(self.client._client.send_goal_async.called)

    def test_duplicate_control_during_save_keeps_original_future(self):
        future=self.client.send_goal('RESTART',request_id='a'*32)
        self.assertIs(self.client.send_goal('RESTART',request_id='a'*32),future)
        self.assertFalse(self.client._client.send_goal_async.called)

# Do not import or inherit test cases into discovery unintentionally.
for case,base in ((ReceiptOrderingTests,BridgeTests),(JournalOrderingTests,AsyncClientStorageTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in case.__dict__:setattr(case,name,None)
del BridgeTests,AsyncClientStorageTests,case,base
