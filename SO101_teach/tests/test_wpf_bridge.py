"""Bridge crash boundaries, fresh UI supervision, and urgent-stop invariants."""
from concurrent.futures import Future
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace,ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'SO101_PC_Package'))
from bridge_service import BridgeService,BridgeError
from supervision_client import ClientHealth,ClientJournal

class Health:
    def __init__(self):self.ready=True;self.acks=[]
    def online(self):return self.ready
    def invalidate(self):self.ready=False
    def envelope(self,*args):return 'test-proof'
    def acknowledge(self,epoch,nonce):self.ready=True;self.acks.append((epoch,nonce));return True
class Arm:
    def __init__(self,name):
        self.arm=name;self.health=Health();self._client=SimpleNamespace(server_is_ready=lambda:True)
        self.journal=SimpleNamespace(value={'job':None,'control':None});self.busy=False;self.feedback='UNKNOWN'
        self.storage_error=None;self.sent=[];self.queries=[]
    def send_goal(self,command,*,request_id,dispatch_guard=None,envelope=None):
        if dispatch_guard is not None:assert dispatch_guard()
        control=command in ('EMER_STOP','RESTART','RESET','CLEAR_ERROR');slot='control' if control else 'job'
        self.journal.value[slot]=dict(command=command,request_id=request_id,state='pending')
        if not control:self.busy=True
        self.sent.append((command,request_id));future=Future();return future
    def query_request(self,command,key,*,control=False):self.queries.append((command,key,control));return Future()

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.now=10.
        self.arms={name:Arm(name) for name in ('arm2','arm3')}
        self.service=BridgeService(self.arms,self.temp.name,clock=lambda:self.now)
        self.connect()
    def connect(self):
        self.session=self.service.session();self.generation=self.session['session_generation'];self.service.attach(self.generation)
        for arm in self.arms.values():arm.health.ready=True
    def request(self,command='BUILD_A',key='1'*32):
        return dict(protocol_version=1,session_generation=self.generation,request_id=key,command=command)
    def test_pc_preparation_is_visible_before_the_pi_goal_is_sent(self):
        key='a'*32;arm=self.arms['arm2']
        self.service.records[key]=self.service.make_record(key,'PC_BUILD_LOAD_A','arm2','UNKNOWN','preparing')
        arm.pc_compute=SimpleNamespace(pending={'key':key});arm.busy=True
        value=self.service.snapshot(self.generation)['arms'][0]['job']
        self.assertEqual(value['request_id'],key);self.assertEqual(value['state'],'ACCEPTED')
        self.assertIn('아직 장치 실행 명령',value['detail']);self.assertEqual(arm.sent,[])

    def test_same_id_and_restarted_bridge_never_repeat_execution(self):
        request=self.request();self.service.submit(request);self.service.submit(request)
        self.assertEqual(len(self.arms['arm2'].sent),1)
        self.service.close(self.generation)
        self.service=BridgeService(self.arms,self.temp.name,clock=lambda:self.now);self.connect()
        self.service.submit(self.request());self.assertEqual(len(self.arms['arm2'].sent),1)
    def test_id_conflict_does_not_dispatch(self):
        self.service.submit(self.request())
        result=self.service.submit(self.request('LOAD_A'))
        self.assertEqual(result['code'],'ID_CONFLICT');self.assertFalse(self.arms['arm3'].sent)
    def test_session_expiration_rejects_queued_old_restart(self):
        old=self.request('RESTART');self.now+=3.1
        with self.assertRaises(BridgeError):self.service.submit(old)
        self.connect()
        with self.assertRaises(BridgeError):self.service.submit(old)
        self.assertFalse(self.arms['arm2'].sent)
    def test_second_operator_and_second_event_stream_are_rejected(self):
        with self.assertRaises(BridgeError):self.service.session()
        with self.assertRaises(BridgeError):self.service.attach(self.generation)
    def test_expired_and_duplicate_ui_ack_do_not_renew_lease(self):
        message=self.service.lease(self.generation);ack=dict(type='lease_ack',session_generation=self.generation,nonce=message['nonce'],protocol_version=1,bridge_instance_id=self.service.instance)
        self.now+=.2;self.assertTrue(self.service.acknowledge(self.generation,ack));deadline=self.service.session_deadline
        self.now+=.2;self.assertFalse(self.service.acknowledge(self.generation,ack));self.assertEqual(deadline,self.service.session_deadline)
        message=self.service.lease(self.generation);ack['nonce']=message['nonce'];self.now+=1.1
        self.assertFalse(self.service.acknowledge(self.generation,ack))
    def test_pi_challenge_requires_current_ui_and_exact_nonce(self):
        self.service.challenge('arm2','epoch','nonce')
        ack=self.service.pending_challenges(self.generation)[0];ack.pop('expires_in_ms')
        bad=dict(ack,nonce='wrong');self.assertFalse(self.service.acknowledge(self.generation,bad))
        self.assertTrue(self.service.acknowledge(self.generation,ack));self.assertFalse(self.service.acknowledge(self.generation,ack))
        self.assertEqual(self.arms['arm2'].health.acks,[('epoch','nonce')])
    def test_slow_storage_does_not_delay_stop_and_fences_pending_motion(self):
        writes=[];self.service.writer=lambda path,value,complete:writes.append((path,value,complete))
        self.service.submit(self.request());self.assertFalse(self.arms['arm2'].sent)
        self.service.submit(self.request('EMER_STOP','2'*32))
        self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','2'*32)])
        writes[0][2](None)
        self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','2'*32)])
    def test_expired_session_during_disk_write_never_dispatches(self):
        writes=[];self.service.writer=lambda path,value,complete:writes.append(complete)
        self.service.submit(self.request());self.now+=3.1;writes[0](None)
        self.assertFalse(self.arms['arm2'].sent)
    def test_storage_failure_blocks_normal_motion_but_not_stop(self):
        with patch('bridge_service.atomic_record',side_effect=OSError('disk full')):
            result=self.service.submit(self.request());self.assertEqual(result['code'],'STORAGE_ERROR')
            self.assertFalse(self.arms['arm2'].sent)
            self.service.submit(self.request('EMER_STOP','2'*32));self.assertEqual(self.arms['arm2'].sent,[('EMER_STOP','2'*32)])
        self.service.maintenance();self.assertFalse(self.service.dirty)
    def test_slow_admission_expires_even_while_operator_session_is_valid(self):
        writes=[];self.service.writer=lambda path,value,complete:writes.append(complete)
        self.service.submit(self.request());self.now+=2.1;writes[0](None)
        self.assertFalse(self.arms['arm2'].sent)
    def test_invalid_version_and_command_types_cannot_dispatch(self):
        for value in (dict(self.request(),protocol_version=True),dict(self.request(),command=[])):
            with self.assertRaises(BridgeError):self.service.submit(value)
        self.assertFalse(self.arms['arm2'].sent)
    def test_pending_receipt_reserves_both_arms(self):
        self.service.writer=lambda *args:None
        self.service.submit(self.request('BUILD_A'))
        self.assertEqual(self.service.submit(self.request('LOAD_A','2'*32))['code'],'NOT_ALLOWED')
    def test_build_done_is_nonterminal_and_stop_has_its_own_slot(self):
        self.service.submit(self.request('BUILD_LOAD_A'));self.arms['arm2'].feedback='BUILD_DONE'
        self.service.submit(self.request('EMER_STOP','2'*32));state=self.service.snapshot(self.generation)['arms'][0]
        self.assertEqual(state['job']['state'],'BUILD_DONE');self.assertEqual(state['control']['command'],'EMER_STOP')
    def test_archived_query_keeps_id_without_execution(self):
        r=self.service.make_record('a'*32,'LOAD_B','arm3','UNKNOWN','old receipt');self.service.records[r['request_id']]=r
        self.service.query(self.generation,'a'*32)
        self.assertEqual(self.arms['arm3'].queries,[('LOAD_B','a'*32,False)]);self.assertFalse(self.arms['arm3'].sent)
    def test_missing_query_never_turns_into_execution(self):
        self.assertIsNone(self.service.query(self.generation,'a'*32));self.assertFalse(self.arms['arm2'].sent)
    def test_missing_api_receipt_is_reconciled_by_query_only(self):
        calls=[]
        def query(command,key,control=False):
            calls.append((command,key));future=Future()
            future.set_result(SimpleNamespace(success=False,message='NOT_FOUND',detail='fenced',accepted=True));return future
        self.arms['arm2'].query_request=query
        result=self.service.query(self.generation,'a'*32,'BUILD_A','arm2')
        self.assertEqual(result['state'],'FAILED');self.assertEqual(result['result_code'],'NOT_FOUND')
        self.assertEqual(calls,[('BUILD_A','a'*32)]);self.assertFalse(self.arms['arm2'].sent)
    def test_missing_stop_receipt_can_query_other_arm_without_execution(self):
        def result(accepted):
            future=Future();future.set_result(SimpleNamespace(success=accepted,message='IDLE' if accepted else 'UNKNOWN',detail='query',accepted=accepted));return future
        self.arms['arm2'].query_request=lambda *args,**kw:result(False)
        self.arms['arm3'].query_request=lambda *args,**kw:result(True)
        value=self.service.query(self.generation,'b'*32,'EMER_STOP','arm2')
        self.assertEqual((value['state'],value['arm']),('SUCCEEDED','arm3'))
        self.assertFalse(self.arms['arm2'].sent+self.arms['arm3'].sent)
    def test_unresolved_control_blocks_new_motion_but_keeps_stop_available(self):
        self.service.submit(self.request('RESET'))
        blocked=self.service.submit(self.request('BUILD_A','2'*32));self.assertFalse(blocked['accepted'])
        self.assertTrue(self.service.submit(self.request('EMER_STOP','3'*32))['accepted'])
    def test_active_arm_uses_submission_time_instead_of_late_query_time(self):
        for name,created in [('arm2',20.),('arm3',10.)]:
            key=('a' if name=='arm2' else 'b')*32
            self.arms[name].journal.value['job']=dict(request_id=key,command='BUILD_A' if name=='arm2' else 'LOAD_A',state='completed',created_at=created,message='IDLE')
        restored=BridgeService(self.arms,self.temp.name,clock=lambda:self.now)
        self.assertEqual(restored.active_arm,'arm2')
    def test_corrupt_receipt_fails_closed(self):
        (Path(self.temp.name)/'bridge-receipts'/'a.json').write_text('broken')
        with self.assertRaises(ValueError):BridgeService(self.arms,self.temp.name)
    def test_existing_client_journal_lock_is_exclusive(self):
        with patch.dict('os.environ',SO101_CLIENT_STATE_DIR=self.temp.name):
            journal=ClientJournal('arm2')
            try:
                with self.assertRaises(RuntimeError):ClientJournal('arm2')
            finally:journal.close()

class SupervisionRelayTests(unittest.TestCase):
    def test_relay_requires_new_ui_ack_and_expiry_is_not_extended(self):
        now=[0.];published=[];forwarded=[]
        modules={name:ModuleType(name) for name in ('std_msgs.msg','rclpy.qos')}
        modules['std_msgs.msg'].String=lambda **kw:SimpleNamespace(**kw)
        modules['rclpy.qos'].QoSProfile=lambda **kw:kw
        modules['rclpy.qos'].ReliabilityPolicy=SimpleNamespace(BEST_EFFORT=1)
        modules['rclpy.qos'].DurabilityPolicy=SimpleNamespace(VOLATILE=1)
        node=SimpleNamespace(create_publisher=lambda *a:SimpleNamespace(publish=lambda m:published.append(json.loads(m.data))),create_subscription=lambda *a:None)
        with patch.dict(sys.modules,modules),patch('supervision_client.time.monotonic',side_effect=lambda:now[0]):
            health=ClientHealth(node,'1'*32,challenge_sink=lambda epoch,nonce:forwarded.append((epoch,nonce)))
            send=lambda v:health.receive(SimpleNamespace(data=json.dumps(v)))
            send(dict(kind='challenge',epoch='e',nonce='n'));self.assertFalse(published);self.assertFalse(health.online())
            self.assertEqual(forwarded,[('e','n')]);self.assertFalse(health.acknowledge('old','n'))
            send(dict(kind='challenge',epoch='e',nonce='fresh'));now[0]=.1
            self.assertTrue(health.acknowledge('e','fresh'));self.assertFalse(health.acknowledge('e','fresh'))
            send(dict(kind='ack',client_id='1'*32,epoch='e',nonce='fresh'));self.assertTrue(health.online())
            now[0]=3.2;self.assertFalse(health.online())
            send(dict(kind='challenge',epoch='e',nonce='expired'));now[0]=4.3
            self.assertFalse(health.acknowledge('e','expired'));self.assertEqual(len(published),1)
            health.invalidate();self.assertFalse(health.online())


class AsyncClientStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_pc_action_client import PCActionClientTests
        PCActionClientTests.setUpClass();cls.fixture_type=PCActionClientTests
    def setUp(self):
        self.fixture=self.fixture_type();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.client=self.fixture.client();self.writes=[]
        self.client.journal_writer=lambda path,value,done:self.writes.append((deepcopy(value),done))
    def test_motion_waits_for_durable_journal_but_stop_does_not(self):
        motion=self.client.send_goal('BUILD_A',request_id='a'*32)
        self.assertFalse(self.client._client.send_goal_async.called)
        self.client.send_goal('EMER_STOP',request_id='b'*32)
        self.assertEqual(self.client._client.send_goal_async.call_count,1)
        self.writes[0][1](None)
        self.assertEqual(self.client._client.send_goal_async.call_count,2)
        self.assertFalse(motion.done())
    def test_stale_dispatch_guard_after_save_never_sends_motion(self):
        future=self.client.send_goal('BUILD_A',request_id='a'*32,dispatch_guard=lambda:False)
        self.writes[0][1](None)
        self.assertFalse(self.client._client.send_goal_async.called)
        self.assertEqual(future.result().message,'UNKNOWN')
    def test_failed_journal_does_not_dispatch_and_stop_still_dispatches(self):
        future=self.client.send_goal('BUILD_A',request_id='a'*32)
        self.writes[0][1](OSError('full'))
        self.assertFalse(self.client._client.send_goal_async.called);self.assertIsNotNone(self.client.storage_error)
        self.assertEqual(future.result().message,'UNKNOWN')
        self.client.send_goal('EMER_STOP',request_id='b'*32)
        self.assertEqual(self.client._client.send_goal_async.call_count,1)
    def test_later_stop_supersedes_pending_restart(self):
        future=self.client.send_goal('RESTART',request_id='a'*32)
        self.client.send_goal('EMER_STOP',request_id='b'*32)
        self.writes[0][1](None)
        self.assertEqual(self.client._client.send_goal_async.call_count,1)
        self.assertEqual(future.result().message,'UNKNOWN')

if __name__=='__main__':unittest.main()
