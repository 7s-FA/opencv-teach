"""PC preparation must not weaken stop, stale intent or ordinary command handling."""
from concurrent.futures import Future
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock,patch
from tests.test_pc_action_client import PCActionClientTests
from pc_compute_client import PcComputePreparation

class Broker:
    gate=None
    instances=[]
    def __init__(self,key,command,*,cancel,config):
        self.key=key;self.cancel=cancel;self.closed=False;Broker.instances.append(self)
    def start(self):
        if self.gate:self.gate.wait(1)
        if self.cancel.is_set():raise RuntimeError('cancelled')
        return self
    def close(self):self.closed=True;self.cancel.set()

class PcAdmissionTests(unittest.TestCase):
    setUpClass=classmethod(PCActionClientTests.setUpClass.__func__)
    setUp=PCActionClientTests.setUp
    client=PCActionClientTests.client
    def configured(self):
        client=self.client();Broker.gate=threading.Event();Broker.instances=[]
        client.pc_compute=PcComputePreparation(client,Future,broker_factory=Broker,configuration=lambda:{})
        return client
    def pump(self,client,until):
        deadline=time.monotonic()+2
        while not until() and time.monotonic()<deadline:client.poll();time.sleep(.005)
        self.assertTrue(until())
    def test_ordinary_chain_never_prepares_gpu(self):
        client=self.configured();client.send_goal('BUILD_LOAD_A')
        self.assertFalse(Broker.instances);client._client.send_goal_async.assert_called_once()
    def test_disabled_pc_does_not_create_or_send_motion_record(self):
        client=self.configured();client.pc_compute.configuration=Mock(side_effect=ValueError('offline validation'))
        future=client.send_goal('PC_BUILD_LOAD_A')
        self.assertEqual(future.result().message,'ERROR');self.assertIsNone(client.journal.value['job'])
        client._client.send_goal_async.assert_not_called();self.assertFalse(client.busy)
    def test_ready_pc_dispatches_once_under_original_business_id(self):
        client=self.configured();key='a'*32;future=client.send_goal('PC_BUILD_LOAD_B',request_id=key)
        self.assertIs(client.send_goal('PC_BUILD_LOAD_B'),future);self.assertTrue(client.busy)
        client._client.send_goal_async.assert_not_called();Broker.gate.set()
        self.pump(client,lambda:bool(client._requests))
        client._client.send_goal_async.assert_called_once();self.assertEqual(client.journal.value['job']['request_id'],key)
        client._finish(client._requests[0],True,'IDLE','done');self.assertTrue(future.result().success)
    def test_stop_during_preparation_never_dispatches_pc_goal(self):
        client=self.configured();future=client.send_goal('PC_BUILD_LOAD_A')
        client.send_goal('EMER_STOP');Broker.gate.set();self.pump(client,future.done)
        self.assertFalse(future.result().success);client._client.send_goal_async.assert_called_once()
        self.assertEqual(client._requests[0]['command'],'EMER_STOP');self.assertIsNone(client.journal.value['job'])
    def test_lost_epoch_or_expired_ui_intent_never_dispatches(self):
        for mode in ('epoch','guard'):
            with self.subTest(mode=mode):
                client=self.configured();client.health.epoch='old';valid=[True]
                future=client.send_goal('PC_BUILD_LOAD_A',dispatch_guard=lambda:valid[0])
                if mode=='epoch':client.health.epoch='new'
                else:valid[0]=False
                Broker.gate.set();self.pump(client,future.done)
                self.assertFalse(future.result().success);client._client.send_goal_async.assert_not_called()
                client.journal.close()
