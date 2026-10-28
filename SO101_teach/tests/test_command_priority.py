import os,fcntl,json,tempfile,threading,unittest,time
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from tests.standby_fixtures import PreparedResources
import standby_preparation
from tests import test_remote as remote_fixtures

class PriorityRuntimeTests(unittest.TestCase):
    setUp=remote_fixtures.RemoteTests.setUp
    tearDown=remote_fixtures.RemoteTests.tearDown
    rpc=remote_fixtures.RemoteTests.rpc
    def test_new_lease_can_take_over_completed_gui_and_cancels_old_timer(self):
        self.rpc('connect',{'speed':350});s=self.runtime.session;s.state='HOLD'
        self.assertTrue(self.rpc('schedule_idle_release')['ok']);old=self.lease
        self.lease=self.runtime.acquire()['lease'];self.assertNotEqual(old,self.lease)
        self.assertIsNone(self.runtime.idle_release.session)
        with self.assertRaises(ValueError):self.runtime.require_lease(old)
    def test_command_and_borrowed_claim_cancel_idle_release(self):
        self.rpc('connect',{'speed':350});s=self.runtime.session;s.state='HOLD'
        self.rpc('schedule_idle_release');self.assertTrue(self.rpc('claim_episode')['ok'])
        self.assertIsNone(self.runtime.idle_release.session)
        self.rpc('schedule_idle_release');self.rpc('command',{'action':'hold'})
        self.assertIsNone(self.runtime.idle_release.session)
    def test_borrowed_unused_peer_preserves_only_an_existing_release(self):
        self.rpc('connect',{'speed':350});s=self.runtime.session;s.state='HOLD'
        self.rpc('claim_episode',{'preserve_idle_release':True})
        self.assertIsNone(self.runtime.idle_release.session)
        self.rpc('schedule_idle_release');self.runtime.idle_release.since=10
        self.rpc('claim_episode',{'preserve_idle_release':True})
        self.assertIs(self.runtime.idle_release.session,s);self.assertIsNone(self.runtime.idle_release.since)
        self.rpc('command',{'action':'hold'});self.assertIsNone(self.runtime.idle_release.session)

    def test_linear_reservation_keeps_release_until_work_unlock_and_new_idle_interval(self):
        from tests.test_reset_settle import ResetSettleTests
        self.runtime.stop.set();self.runtime.watch.join(1)
        self.rpc('connect',{'speed':350});s=self.runtime.session;s.state='HOLD'
        self.rpc('schedule_idle_release')
        with tempfile.TemporaryDirectory() as directory,patch.object(self.runtime,'data_dir',Path(directory)),patch.object(s,'request') as request:
            with (Path(directory)/'episode-cli.lock').open('a') as work:
                fcntl.flock(work,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.lease=self.runtime.acquire(preserve_idle_release=True)['lease']
                self.assertIs(self.runtime.idle_release.session,s)
                s.latest=ResetSettleTests().session(100).latest
                with patch('so101_teach.remote_server.time.monotonic',return_value=100):self.runtime.poll_idle_release()
                request.assert_not_called();self.assertIsNone(self.runtime.idle_release.since)
                self.runtime.detach(self.lease)
            for at in (101,103.99):
                s.latest=ResetSettleTests().session(at).latest
                with patch('so101_teach.remote_server.time.monotonic',return_value=at):self.runtime.poll_idle_release()
                request.assert_not_called()
            s.latest=ResetSettleTests().session(104).latest
            with patch('so101_teach.remote_server.time.monotonic',return_value=104):self.runtime.poll_idle_release()
            request.assert_called_once_with('release')

    def test_linear_reservation_does_not_create_release_for_manual_hold_or_off(self):
        self.rpc('connect',{'speed':350});s=self.runtime.session
        for state in ('HOLD','READ_ONLY'):
            s.state=state;self.runtime.detach(self.lease)
            self.lease=self.runtime.acquire(preserve_idle_release=True)['lease']
            self.assertIsNone(self.runtime.idle_release.session)
            self.assertFalse(s.release_requested.is_set())

    def test_hold_after_linear_handoff_cancels_preserved_release(self):
        self.rpc('connect',{'speed':350});self.runtime.session.state='HOLD'
        self.rpc('schedule_idle_release');self.lease=self.runtime.acquire(preserve_idle_release=True)['lease']
        self.rpc('command',{'action':'hold'})
        self.assertIsNone(self.runtime.idle_release.session)

    def test_cli_link_passes_release_intent_through_authenticated_http(self):
        from so101_teach.remote_server import Server,Handler
        self.rpc('connect',{'speed':350});self.runtime.session.state='HOLD';self.rpc('schedule_idle_release')
        server=Server(('127.0.0.1',0),Handler);server.runtime=self.runtime;server.token='test'
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        link=cli.Link('arm2');link.preserve_idle_release=True
        try:
            with patch.object(cli.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps({'port':server.server_port,'token':'test'}))):link.open()
            self.assertIs(self.runtime.idle_release.session,self.runtime.session)
            self.assertEqual(self.runtime.lease,link.lease)
            self.assertFalse(self.runtime.session.release_requested.is_set())
        finally:
            link.close();server.shutdown();server.server_close();thread.join(1)

class PriorityLifecycleTests(unittest.TestCase):
    def test_linear_commands_prepare_both_arms_and_commit_standby_before_unlock(self):
        import linear_client
        for command in ('slide_build','slide_load'):
            links=[]
            class Link(FakeLink):
                def __init__(self,arm):super().__init__();self.arm=arm;links.append(self)
                def open(self):self.lease='lease';self.open_preserve=self.preserve_idle_release
                def close(self):self.lease=None
            class Linear(FakeLinear):
                def open(self):pass
                def close(self):pass
            with patch.object(standby_preparation,'WorkcellPreparation',PreparedResources),tempfile.TemporaryDirectory() as directory,patch.object(cli,'ROOT',Path(directory)),patch.object(cli,'Link',Link),patch.object(cli.signal,'signal'),patch.object(linear_client,'LinearClient',Linear):
                (cli.ROOT/'data').mkdir()
                self.assertEqual(cli.main([command,'--no-ros']),0)
            self.assertEqual(len(links),2);self.assertTrue(all(link.open_preserve for link in links))
            self.assertTrue(all(any(method=='standby_complete' for method,_ in link.calls) for link in links))
            self.assertFalse(any(method in ('standby_refresh','schedule_idle_release') for link in links for method,_ in link.calls))
    def test_final_status_cannot_overwrite_a_newer_job(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(cli,'ROOT',Path(directory)):
            (Path(directory)/'data').mkdir();old=cli.Status('arm2','A',False);old.emit('ACCEPTED');old.emit('DONE')
            new=cli.Status('arm2','B',False);new.emit('ACCEPTED');old.flush_final()
            self.assertEqual(json.loads(new.path.read_text())['run_id'],new.run_id)
            self.assertEqual(old.events[-1]['status'],'A_DONE')
    def test_chain_handoff_has_no_release_and_done_follows_resource_unlock(self):
        import linear_client
        order=[];links=[];published=[]
        class Link(FakeLink):
            def __init__(self,arm):super().__init__();self.arm=arm;links.append(self)
            def open(self):self.lease='lease'
            def close(self):order.append('close-'+self.arm);self.lease=None
            def rpc(self,method,args=None):
                order.append((self.arm,method));return super().rpc(method,args)
        class Linear(FakeLinear):
            def open(self):pass
            def close(self):order.append('linear-close')
        class Runner:
            def __init__(self,link,emit,*args,**kwargs):self.link=link;self.emit=emit;self.safe_ticks={}
            def execute(self,*args):
                if getattr(self,'chain_start',None):self.chain_start['receipts']={'arm2':{},'arm3':{}}
                order.append('safe-'+self.link.arm);self.emit('DONE')
            def check(self):pass
            def emergency(self):raise AssertionError('unexpected failure')
        original_publish=cli.Status.publish
        def publish(status,suffix):
            if suffix=='CHAIN_DONE':
                with (cli.ROOT/'data/episode-cli.lock').open('a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertTrue(all(link.lease is None for link in links));self.assertIn('linear-close',order)
            published.append(suffix);original_publish(status,suffix)
        with patch.object(standby_preparation,'WorkcellPreparation',PreparedResources),tempfile.TemporaryDirectory() as directory,patch.object(cli,'ROOT',Path(directory)),patch.object(cli,'Link',Link),patch.object(cli,'Runner',Runner),patch.object(cli.signal,'signal'),patch.object(linear_client,'LinearClient',Linear),patch.object(cli.Status,'publish',publish):
            (cli.ROOT/'data').mkdir();ep={'id':'priority-test','calibration_sha256':'test-calibration','steps':[{'id':'start','name':'start','safe_boundary':'start','ticks':{name:0 for name in ('shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper')}},{'id':'placed','ticks':{},'name':'placed','inspection':{'station':'finished','target':'pallet','expected':'cap_added','timeout_seconds':10}},{'id':'end','ticks':{},'name':'end','safe_boundary':'end'}]}
            with patch.object(cli,'load_job',return_value=(cli.ROOT/'data',ep,{},100,30)):
                fd=os.open(cli.ROOT/'data/episode-cli.lock',os.O_CREAT|os.O_WRONLY,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(cli.main(['build_load_a','--no-ros','--execution-lock-fd',str(fd)]),0)
        self.assertLess(order.index('safe-arm2'),order.index('safe-arm3'))
        self.assertLess(order.index('safe-arm3'),order.index(('arm2','standby_complete')))
        self.assertNotIn('DONE',published);self.assertEqual(published[-1],'CHAIN_DONE')
        self.assertFalse(any(m=='command' and a.get('action')=='release' for link in links for m,a in link.calls))
