"""Reproduce safety boundary races without ROS transport or any device access."""
import json,os,queue,threading,time
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import unittest
from tests.test_action_contract import ActionContractTests as Fixture,Goal,protocol
from tests.test_episode_cli import cli
from supervision_session import SupervisionHealth,decode_goal
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'SO101_PC_Package'))
from supervision_client import ClientHealth

class Once:
    def __init__(self):self.done=False
    def wait(self,seconds):
        if self.done:return True
        self.done=True;return False

class SafetyRaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):Fixture.setUpClass()
    def setUp(self):
        self.f=Fixture();self.f.setUp();self.addCleanup(self.f.doCleanups);self.s=self.f.server
        p=patch.object(cli,'ROOT',self.f.module.ROOT);p.start();self.addCleanup(p.stop)
    def health(self,connected=False):
        h=object.__new__(SupervisionHealth);h.gate=self.s.gate;h.notice=Mock();h.notifications=queue.Queue(64)
        h.stop=Once();h.connected=connected;h.fresh=lambda client:h.connected;h.accepts=lambda meta:h.connected;h.control_fence=lambda meta,state:True
        self.s.health=h;return h
    def test_late_restart_after_processed_hold_is_rejected_in_executor(self):
        s=self.s;ticket=s.gate.reserve(client_id='c'*32);ticket.started=True
        control=cli.Control();control.job_id=ticket.job_id;control.paused=True;control.pause.set()
        goal=Goal('RESTART');self.assertTrue(s.goal_callback(goal.request))
        def delayed(root,command,**kwargs):
            s.gate.stop_active();self.assertEqual(os.read(ticket.read_fd,1),b'!');control.command('estop')
            return json.loads(control.command('control '+json.dumps(dict(job_id=ticket.job_id,request_id=kwargs['request_id'],command=command,stop_generation=kwargs['stop_generation']))))
        with patch.object(protocol,'control_state',return_value={'job_id':ticket.job_id,'stop_generation':0}),patch.object(protocol,'control_request',side_effect=delayed),patch.object(protocol,'wait_control') as wait:
            self.assertFalse(s.execute_callback(goal).success);wait.assert_not_called()
        self.assertFalse(control.resume.is_set());self.assertTrue(control.pause.is_set())
    def test_reset_rearms_watch_before_waiting_for_safe_return(self):
        s=self.s;h=self.health();ticket=s.gate.reserve(client_id='c'*32);ticket.started=True
        h.watch();self.assertEqual(os.read(ticket.read_fd,1),b'!');self.assertTrue(ticket.link_lost)
        h.connected=True;goal=Goal('RESET');self.assertTrue(s.goal_callback(goal.request))
        def wait(*args):
            self.assertFalse(ticket.link_lost)
            h.connected=False;h.stop=Once();h.watch()
            self.assertEqual(os.read(ticket.read_fd,1),b'!');return False
        with patch.object(protocol,'control_state',return_value={'job_id':ticket.job_id,'stop_generation':1}),patch.object(protocol,'control_request',return_value={'request_id':'d'*32}),patch.object(protocol,'wait_control',side_effect=wait):
            self.assertFalse(s.execute_callback(goal).success)
        self.assertTrue(ticket.link_lost)
    def test_storage_stall_does_not_block_watch_or_urgent_stop(self):
        s=self.s;h=self.health(True);h.stop=threading.Event();ticket=s.gate.reserve(client_id='c'*32);ticket.started=True
        entered=threading.Event();release=threading.Event()
        def stalled(*args):entered.set();release.wait(2)
        with patch.object(s.ledger,'accept',side_effect=stalled):
            admission=threading.Thread(target=s.goal_callback,args=(Goal('RESET').request,));admission.start()
            watcher=None
            try:
                self.assertTrue(entered.wait(1));h.connected=False
                watcher=threading.Thread(target=h.watch);watcher.start()
                until=time.monotonic()+.5
                while not ticket.stopped and time.monotonic()<until:time.sleep(.005)
                self.assertTrue(ticket.stopped);self.assertTrue(admission.is_alive())
                self.assertEqual(os.read(ticket.read_fd,1),b'!');h.notice.assert_not_called()
            finally:
                release.set();admission.join(1);h.stop.set()
                if watcher:watcher.join(1)
    def test_stop_during_durable_admission_cancels_late_control(self):
        goal=Goal('RESET');s=self.s;accept=s.ledger.accept
        def stop_during_write(*args):s.gate.stop_active();return accept(*args)
        with patch.object(s.ledger,'accept',side_effect=stop_during_write):self.assertTrue(s.goal_callback(goal.request))
        with patch.object(s,'invoke_control') as invoke:
            self.assertFalse(s.execute_callback(goal).success);invoke.assert_not_called()
    def test_fence_requires_current_generation_and_replays_without_resuming(self):
        c=cli.Control();c.paused=True;c.command('estop');key='a'*32
        def request(generation,request_id=key):return c.command('control '+json.dumps(dict(job_id=c.job_id,request_id=request_id,command='restart',stop_generation=generation)))
        with self.assertRaisesRegex(ValueError,'STALE_CONTROL'):request(0)
        first=request(1);self.assertTrue(c.resume.is_set());c.command('estop')
        request(1);self.assertFalse(c.resume.is_set())
        with self.assertRaisesRegex(ValueError,'STALE_CONTROL'):request(1,'b'*32)
        request(2,'c'*32);self.assertTrue(c.resume.is_set())

class MalformedMessages(unittest.TestCase):
    def test_bad_tokens_are_ignored_on_both_peers_and_rejected_as_goals(self):
        h=object.__new__(SupervisionHealth);h.lock=threading.RLock();h.challenges={};h.epoch='e';h.seen={};h.publisher=Mock();h.String=SimpleNamespace
        pc=object.__new__(ClientHealth);pc.lock=threading.RLock();pc.client_id='c'*32;pc.epoch='e';pc.sent={};pc.last_ack=float('-inf');pc.publisher=Mock()
        for token in ([],{},3,True,'x'*129):
            with self.subTest(token=type(token).__name__):
                h.receive(SimpleNamespace(data=json.dumps(dict(client_id='c'*32,epoch='e',nonce=token))))
                self.assertFalse(h.accepts(dict(client_id='c'*32,epoch='e',nonce=token)))
                pc.receive(SimpleNamespace(data=json.dumps(dict(kind='ack',client_id='c'*32,epoch='e',nonce=token))))
                pc.receive(SimpleNamespace(data=json.dumps(dict(kind='challenge',epoch=token,nonce='n'))))
                goal=Goal('RESTART');goal.meta['nonce']=token
                with self.assertRaises(ValueError):decode_goal(json.dumps(goal.meta))
        self.assertFalse(pc.online());h.publisher.publish.assert_not_called();pc.publisher.publish.assert_not_called()
