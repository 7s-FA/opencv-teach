import os,fcntl,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
from tests.test_episode_cli import cli,FakeLink,FakeLinear
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

class PriorityLifecycleTests(unittest.TestCase):
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
            def execute(self,*args):order.append('safe-'+self.link.arm);self.emit('DONE')
            def check(self):pass
            def emergency(self):raise AssertionError('unexpected failure')
        original_publish=cli.Status.publish
        def publish(status,suffix):
            if suffix=='CHAIN_DONE':
                with (cli.ROOT/'data/episode-cli.lock').open('a') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertTrue(all(link.lease is None for link in links));self.assertIn('linear-close',order)
            published.append(suffix);original_publish(status,suffix)
        with tempfile.TemporaryDirectory() as directory,patch.object(cli,'ROOT',Path(directory)),patch.object(cli,'Link',Link),patch.object(cli,'Runner',Runner),patch.object(cli.signal,'signal'),patch.object(linear_client,'LinearClient',Linear),patch.object(cli.Status,'publish',publish):
            (cli.ROOT/'data').mkdir();ep={'steps':[{'ticks':{},'name':'end'}]}
            with patch.object(cli,'load_job',return_value=(cli.ROOT/'data',ep,{},100,30)):
                fd=os.open(cli.ROOT/'data/episode-cli.lock',os.O_CREAT|os.O_WRONLY,0o600);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertEqual(cli.main(['build_load_a','--no-ros','--execution-lock-fd',str(fd)]),0)
        self.assertLess(order.index('safe-arm2'),order.index('safe-arm3'))
        self.assertLess(order.index('safe-arm3'),order.index(('arm2','schedule_idle_release')))
        self.assertNotIn('DONE',published);self.assertEqual(published[-1],'CHAIN_DONE')
        self.assertFalse(any(m=='command' and a.get('action')=='release' for link in links for m,a in link.calls))
