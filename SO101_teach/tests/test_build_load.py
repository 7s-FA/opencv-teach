import tempfile,unittest,fcntl
from pathlib import Path
from unittest.mock import patch
from tests.test_episode_cli import cli
import linear_client
import episode_recovery
from types import SimpleNamespace

class BuildLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'data').mkdir();self.events=[]
    def run_command(self,*,fail=None,command='build_load_b',check=False):
        events=self.events;root=self.root
        class Link:
            def __init__(self,arm):self.arm=arm;self.lease=None
            def open(self):self.lease='test';events.append(('open',self.arm))
            def close(self):self.lease=None
            def rpc(self,*args):pass
        class Linear:
            def open(self):pass
            def close(self):pass
        class Runner:
            def __init__(self,link,emit,control,timeout,**kw):self.link=link;self.emit=emit;self.safe_ticks={};self.phase=len([e for e in events if e[0]=='execute']);self.assembly_evidence=SimpleNamespace(receipt={'episode_id':'current-build','completed_at':123.})
            def execute(self,ep,bundle,speed,target):
                with (root/'data/episode-cli.lock').open('a') as lock:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:pass
                    else:raise AssertionError('combined workcell lock was released')
                events.append(('execute',self.link.arm,target))
                if self.link.arm=='arm3':events.append(('history_required',hasattr(self,'chain_handoff') or hasattr(self,'assembly_handoff')))
                if fail==self.link.arm:raise RuntimeError('inspection failed')
                if fail=='reset':raise cli.ResetRequested('reset')
                self.emit('DONE')
            def check(self):
                if fail=='boundary':raise cli.ResetRequested('reset between phases')
            def reset_to_safe(self):events.append(('reset',self.link.arm))
            def emergency(self):events.append(('hold',self.link.arm))
        def load(arm,letter):
            events.append(('validate',arm,letter))
            if fail=='load_validation' and arm=='arm3':raise ValueError('invalid load')
            ep=dict(id='e',name=letter,calibration_sha256='cal',steps=[dict(id='s',ticks={},name='safe',safe_boundary='end')])
            return root,ep,{},300,30
        def reset_all(api,records,links,linear,control,emit,**kw):
            events.append(('reset',kw['first']));events.append(('reset','arm3'));emit('RESET_DONE')
        with patch.object(episode_recovery,'workcell_records',return_value={}),patch.object(episode_recovery,'reset_workcell',side_effect=reset_all),patch.object(cli,'ROOT',root),patch.object(cli,'load_job',side_effect=load),patch.object(cli,'Link',Link),patch.object(cli,'Runner',Runner),patch.object(linear_client,'LinearClient',Linear),patch.object(cli,'control_server',side_effect=lambda control,done:done.wait(2)),patch.object(cli.signal,'signal'):
            return cli.main([command,'--no-ros']+(['--check'] if check else []))
    def test_build_finishes_before_load_and_lock_is_retained(self):
        self.assertEqual(self.run_command(),0)
        self.assertEqual([e for e in self.events if e[0]=='execute'],[('execute','arm2',100.),('execute','arm3',1.5)])
        self.assertEqual(self.events[:2],[('validate','arm2','B'),('validate','arm3','B')])
        self.assertIn(('history_required',False),self.events)
    def test_build_failure_never_starts_load(self):
        self.assertEqual(self.run_command(fail='arm2'),1)
        self.assertEqual([e[1] for e in self.events if e[0]=='execute'],['arm2'])
    def test_reset_at_handoff_cancels_remaining_load(self):
        self.assertEqual(self.run_command(fail='boundary'),130)
        self.assertEqual([e[1] for e in self.events if e[0]=='execute'],['arm2']);self.assertIn(('reset','arm2'),self.events)
    def test_bad_load_is_rejected_before_any_device_connection(self):
        self.assertEqual(self.run_command(fail='load_validation'),1);self.assertFalse(any(e[0]=='open' for e in self.events))
    def test_check_validates_both_products_without_devices(self):
        for command in ('build_load_a','build_load_b'):
            self.events=[];self.assertEqual(self.run_command(command=command,check=True),0)
            self.assertEqual(len(self.events),2);self.assertTrue(all(e[0]=='validate' for e in self.events))
