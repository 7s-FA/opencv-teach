"""Repeated stop evidence and inactive-session scope; never access hardware."""
import json,tempfile,time,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear

class StopRequestScopeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=patch.object(cli,'ROOT',Path(temp.name));root.start();self.addCleanup(root.stop)
        self.control=cli.Control()
    def request(self,key='a',command='estop'):
        c=self.control
        return json.loads(c.command('control '+json.dumps(dict(job_id=c.job_id,request_id=key*32,
                          command=command,stop_generation=c.stop_generation))))
    def test_repeat_stop_stays_pending_until_new_feedback_and_blocks_early_restart(self):
        c=self.control;c.paused=True;link=FakeLink();link.mode='HOLD';old=link.state()
        receipt=self.request();self.assertEqual(receipt['state'],'pending')
        self.assertFalse(c.confirm_estop([old]))
        with self.assertRaisesRegex(ValueError,'CONTROL_PENDING'):self.request('b','restart')
        self.assertTrue(c.confirm_estop([link.state()]));self.assertEqual(c.receipts['a'*32]['state'],'completed')
        self.assertEqual(self.request()['state'],'completed')
        self.request('b','restart');self.assertTrue(c.resume.is_set())
    def test_repeated_stop_with_stale_feedback_cannot_survive_cleanup_as_completed(self):
        c=self.control;link=FakeLink();link.mode='HOLD';replies=[]
        def emit(event):
            if event=='PAUSED':link.stale=True;replies.append(self.request('b'))
        runner=cli.Runner(link,emit,c,20,linear=FakeLinear());self.request()
        with self.assertRaisesRegex(RuntimeError,'자세 유지 상태 변경'):runner.await_pause()
        self.assertEqual(replies[0]['state'],'pending')
        with self.assertRaisesRegex(RuntimeError,'시간 초과'):cli.finish_controls(c,[link],timeout=.01)
        self.assertEqual(c.receipts['b'*32]['state'],'failed')
    def test_paused_loop_completes_repeated_stop_on_fresh_feedback(self):
        c=self.control;link=FakeLink();link.mode='HOLD';replies=[]
        def emit(event):
            if event=='PAUSED':replies.append(self.request('b'))
        runner=cli.Runner(link,emit,c,20,linear=FakeLinear());original=c.confirm_estop
        def confirm(states):
            result=original(states)
            if c.receipts.get('b'*32,{}).get('state')=='completed':c.resume.set()
            return result
        self.request()
        with patch.object(c,'confirm_estop',side_effect=confirm):runner.await_pause()
        self.assertEqual(replies[0]['state'],'pending');self.assertEqual(c.receipts['b'*32]['state'],'completed')
        self.assertTrue(all(args.get('action')=='hold' for method,args in link.calls if method=='command'))
    def test_failed_hold_request_is_not_reported_complete(self):
        self.control.paused=True;self.control.emergency=lambda:['read timeout']
        with self.assertRaisesRegex(RuntimeError,'정지 요청 실패'):self.request()
        self.assertNotEqual(self.control.receipts.get('a'*32,{}).get('state'),'completed')
        self.assertTrue(self.control.pause.is_set())
    def closed(self):
        link=FakeLink();value=link.state();value['running']=False;value['latest']['monotonic']-=10
        link.initial_inactive_follower=deepcopy(value);link.state=lambda:deepcopy(value)
        return link,value
    def test_unchanged_preexisting_closed_peer_does_not_fail_active_arm_stop(self):
        self.request();active=FakeLink();active.mode='HOLD';peer,_=self.closed()
        cli.finish_controls(self.control,[active,peer]);self.assertEqual(self.control.receipts['a'*32]['state'],'completed')
        self.assertEqual(peer.calls,[])
    def test_changed_or_unproven_closed_session_is_not_silently_ignored(self):
        for change in ('new_session','unknown_baseline','torque_on','error'):
            self.control=cli.Control();self.request();peer,value=self.closed()
            if change=='new_session':value['latest']['monotonic']+=1
            if change=='unknown_baseline':peer.initial_inactive_follower=None
            if change=='torque_on':value['latest']['telemetry']['0']['torque']=1
            if change=='error':value['error']='OFF unconfirmed'
            with self.subTest(change=change),self.assertRaisesRegex(RuntimeError,'연결 종료'):cli.finish_controls(self.control,[peer])
            self.assertEqual(self.control.receipts['a'*32]['state'],'failed')
    def test_real_link_captures_baseline_and_invalidates_before_connect_attempt(self):
        _,closed=self.closed();link=cli.Link('arm3')
        def post(path,body=None,**kw):
            if path=='/acquire':return {'lease':'test'}
            if path=='/state':return {'follower':deepcopy(closed)}
            if path=='/rpc' and body['method']=='execution_state':return deepcopy(closed)
            if path=='/rpc':raise OSError('connect ACK lost')
            return {}
        metadata=SimpleNamespace(returncode=0,stdout=json.dumps(dict(port=123,token='synthetic')),stderr='')
        thread=SimpleNamespace(start=lambda:None,join=lambda *a:None)
        with patch.object(link,'post',side_effect=post),patch.object(cli.subprocess,'run',return_value=metadata),patch.object(cli.threading,'Thread',return_value=thread):
            link.open();self.assertEqual(link.initial_inactive_follower,closed)
            with self.assertRaises(OSError):link.rpc('connect')
            self.assertIsNone(link.initial_inactive_follower)
    def test_observed_running_session_invalidates_old_closed_baseline(self):
        peer,closed=self.closed();link=cli.Link('arm3');link.initial_inactive_follower=closed
        with patch.object(link,'post',return_value=FakeLink().state()):link.state()
        self.assertIsNone(link.initial_inactive_follower)

    def test_first_stop_publishes_paused_before_completion_is_observable(self):
        c=self.control;self.request();link=FakeLink();original=c.save_receipts;seen=[]
        def save():
            if c.receipts['a'*32]['state']=='completed':seen.append(c.paused)
            original()
        with patch.object(c,'save_receipts',side_effect=save):self.assertTrue(c.confirm_estop([link.state()]))
        self.assertEqual(seen,[True]);self.request('b','restart');self.assertTrue(c.resume.is_set())

    def test_missing_state_after_connect_attempt_is_not_an_unused_arm(self):
        link=cli.Link('arm3')
        with patch.object(link,'post',return_value=None):self.assertIsNone(link.state())
        with patch.object(link,'post',side_effect=OSError('ACK lost')):
            with self.assertRaises(OSError):link.rpc('connect')
        with patch.object(link,'post',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'사용한 팔로워 상태'):link.state()
