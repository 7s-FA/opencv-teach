"""Reproduce a PC leader timeout while the Pi follower stays connected."""
import threading,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from so101_teach.domain import Snapshot
from so101_teach.remote_client import RemoteLink,RemoteMotionSession
from so101_teach.remote_server import LeaderBridge
from so101_teach.motion import SPEED_PRESETS
from tests import test_remote as fixtures

class TransportTests(unittest.TestCase):
    setUp=fixtures.RemoteTests.setUp
    tearDown=fixtures.RemoteTests.tearDown
    rpc=fixtures.RemoteTests.rpc
    def connected(self):
        result=self.rpc('connect',{'speed':SPEED_PRESETS['보통']});self.assertTrue(result['ok'],result)
        self.runtime.session.state='HOLD';self.runtime.leader=LeaderBridge(self.runtime.cal)
        link=RemoteLink({'host':'localhost','user':'robot'});link.lease=self.lease;link.instance=self.runtime.instance
        proxy=RemoteMotionSession(link,'Pi USB',self.runtime.cal);proxy.running=True;link.listeners['follower']=proxy
        now=time.monotonic();link.clock_anchor=(now,now);link.latest_state=self.runtime.state(self.lease)
        reader=SimpleNamespace(running=True,latest=Snapshot('leader',self.runtime.ref.middle.copy(),{},now,time.time(),self.runtime.cal.sha256,True,'PC USB'),close=Mock(),publish=Mock())
        link.leader_client=reader
        return link,proxy,reader
    def test_one_leader_timeout_does_not_close_follower_and_next_sample_recovers(self):
        link,proxy,reader=self.connected();payloads=[]
        def request(path,body,timeout):
            self.assertEqual(path,'/leader');payloads.append(body)
            if len(payloads)==1:
                ticks=reader.latest.ticks.copy();ticks['shoulder_pan']+=5
                reader.latest=Snapshot('leader',ticks,{},time.monotonic(),time.time(),self.runtime.cal.sha256,True,'PC USB')
                raise TimeoutError('timed out')
            result=self.runtime.leader_sample(body);link.stop.set();return result
        link.http=request;link.poll_leader()
        self.assertIsNone(link.error);self.assertIsNone(link.leader_upload_error);self.assertTrue(proxy.running)
        reader.close.assert_not_called();self.assertEqual(len(payloads),2)
        self.assertGreater(payloads[1]['sequence'],payloads[0]['sequence'])
        self.assertEqual(payloads[1]['sample']['ticks']['shoulder_pan'],payloads[0]['sample']['ticks']['shoulder_pan']+5)
        self.assertEqual(self.runtime.leader.latest.ticks,reader.latest.ticks);self.assertEqual(self.runtime.session.state,'HOLD')
        self.assertEqual([e['channel'] for e in link.transport_events],['leader','leader'])
        self.assertTrue(self.runtime.session.commands.empty())
    def test_episode_suppresses_leader_uploads_then_hold_resumes_latest_sample(self):
        link,proxy,reader=self.connected();link.latest_state['follower']['state']='MOVING';arrived=threading.Event()
        link.http=Mock(side_effect=lambda *a,**kw:(arrived.set(),{'accepted':True})[1])
        thread=threading.Thread(target=link.poll_leader,daemon=True);thread.start()
        try:
            self.assertFalse(arrived.wait(.09));link.http.assert_not_called()
            link.latest_state['follower']['state']='HOLD';self.assertTrue(arrived.wait(.3))
            self.assertIsNone(link.error);self.assertTrue(proxy.running)
        finally:link.stop.set();thread.join(1)
    def test_terminal_link_failure_cannot_be_hidden_by_a_late_healthy_response(self):
        link,proxy,reader=self.connected();state=self.runtime.state(self.lease)['follower']
        link.fail('control channel lost');proxy.consume(state,0,time.monotonic())
        self.assertTrue(link.stop.is_set());self.assertFalse(proxy.running);self.assertEqual(proxy.error,'control channel lost')
        reader.close.assert_called_once()
    def test_healthy_state_updates_running_flag_in_both_directions(self):
        link,proxy,reader=self.connected();state=self.runtime.state(self.lease)['follower']
        proxy.running=False;proxy.consume(state,0,time.monotonic());self.assertTrue(proxy.running);self.assertEqual(proxy.state,'HOLD')
        state['running']=False;proxy.consume(state,0,time.monotonic());self.assertFalse(proxy.running)
    def test_inflight_state_reply_after_failure_does_not_restart_heartbeat_loop(self):
        link,proxy,reader=self.connected();result=self.runtime.state(self.lease)
        def request(*args,**kw):link.fail('connection failed elsewhere');return result
        link.http=Mock(side_effect=request);link.poll_state()
        self.assertEqual(link.http.call_count,1);self.assertFalse(proxy.running);self.assertEqual(proxy.error,'connection failed elsewhere')

    def test_transient_status_timeout_keeps_follower_connected(self):
        link,proxy,reader=self.connected();result=self.runtime.state(self.lease)
        link.http=Mock(side_effect=[TimeoutError('brief delay'),result])
        consume=proxy.consume
        def stop_after_sample(*args):consume(*args);link.stop.set()
        proxy.consume=stop_after_sample;link.poll_state()
        self.assertEqual(link.http.call_count,2);self.assertIsNone(link.error);self.assertTrue(proxy.running)
        self.assertEqual([e['channel'] for e in link.transport_events],['state','state'])
        reader.close.assert_not_called()
    def test_long_status_outage_stops_heartbeats_without_sending_motor_commands(self):
        link,proxy,reader=self.connected();clock=[0.]
        def offline(*args,**kw):clock[0]=2.1;raise TimeoutError('offline')
        link.http=Mock(side_effect=offline)
        self.runtime.stop.set();self.runtime.watch.join(.2)
        with patch('so101_teach.remote_client.time.monotonic',side_effect=lambda:clock[0]):link.poll_state()
        self.assertEqual(link.http.call_count,1);self.assertTrue(link.stop.is_set());self.assertFalse(proxy.running)
        self.assertIn('2초',link.error);self.assertTrue(self.runtime.session.commands.empty())
    def test_unknown_motion_rpc_result_is_never_automatically_replayed(self):
        link,proxy,reader=self.connected();link.http=Mock(side_effect=TimeoutError('unknown command outcome'))
        with self.assertRaises(ConnectionError):link.rpc('command',{'action':'play','targets':[self.runtime.ref.middle]})
        self.assertEqual(link.http.call_count,1);self.assertTrue(link.stop.is_set())
    def test_reviewed_client_only_update_accepts_only_exact_version_pair(self):
        from so101_teach.remote_client import server_version_supported
        from so101_teach.domain import atomic_json
        from pathlib import Path
        path=Path(self.tmp.name)/'compatibility.json'
        atomic_json(path,{'schema':1,'client_code_version':'new','server_code_version':'old'})
        self.assertTrue(server_version_supported('old',1,current='new',compatibility=path))
        self.assertFalse(server_version_supported('different-server',1,current='new',compatibility=path))
        self.assertFalse(server_version_supported('old',1,current='another-client',compatibility=path))
        self.assertFalse(server_version_supported('old',2,current='new',compatibility=path))
    def test_missing_or_invalid_compatibility_does_not_relax_version_check(self):
        from so101_teach.remote_client import server_version_supported
        from pathlib import Path
        path=Path(self.tmp.name)/'missing.json'
        self.assertFalse(server_version_supported('old',1,current='new',compatibility=path))
        path.write_text('not JSON')
        self.assertFalse(server_version_supported('old',1,current='new',compatibility=path))
        self.assertTrue(server_version_supported('same',1,current='same',compatibility=path))
