"""Exercise the real runner and paired coordinator with device-free links."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear


class ChainParallelStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.patch=patch.object(cli,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.trace=[];trace=self.trace
        class Link(FakeLink):
            def __init__(self,arm):super().__init__();self.arm=arm
            def rpc(self,method,args=None):
                if method=='command':trace.append((self.arm,args['action']))
                return super().rpc(method,args)
        self.links={a:Link(a) for a in ('arm2','arm3')};self.linear=FakeLinear();self.control=cli.Control();self.events=[]
        self.episodes={a:{'id':a,'steps':[{'id':'start','name':'start','safe_boundary':'start','ticks':{str(i):2048 for i in range(6)}},
            {'id':'work','name':'work','ticks':{str(i):2050 for i in range(6)}},
            {'id':'end','name':'end','safe_boundary':'end','ticks':{str(i):2051 for i in range(6)}}]} for a in self.links}
        self.bundles={a:{'profile':{'robot_id':a}} for a in self.links}
        self.shared={'links':self.links,'records':cli.chain_start_records({a:(self.root,self.episodes[a],self.bundles[a],300,20) for a in self.links})}
    def runner(self,arm):
        return cli.Runner(self.links[arm],self.events.append,self.control,20,peer=self.links['arm3' if arm=='arm2' else 'arm2'],linear=self.linear)
    def build(self):
        run=self.runner('arm2');run.chain_start=self.shared
        run.execute(self.episodes['arm2'],self.bundles['arm2'],300,100.)
        return run
    def test_pair_moves_before_build_and_load_does_not_repeat_safe_move(self):
        before=deepcopy(self.episodes);run=self.build()
        self.assertEqual(self.trace[:4],[('arm2','arm'),('arm3','arm'),('arm2','move'),('arm3','move')])
        self.assertLess(self.events.index('CHAIN_SAFE_DONE'),self.events.index('EPISODE_STARTED'))
        self.assertEqual(run.safe_ticks,self.episodes['arm2']['steps'][-1]['ticks'])
        load=self.runner('arm3');load.prepared_start=self.shared['receipts']['arm3']
        load.execute(self.episodes['arm3'],self.bundles['arm3'],300,1.5)
        self.assertEqual([x for x in self.trace if x[1]=='move'],[('arm2','move'),('arm3','move')])
        self.assertEqual(self.episodes,before)
        self.assertFalse(list((self.root/'data').glob('episode-recovery-*.json')))
    def test_changed_parked_arm_is_rejected_before_load_motion(self):
        self.build();self.links['arm3'].completed=-1
        load=self.runner('arm3');load.prepared_start=self.shared['receipts']['arm3']
        with self.assertRaisesRegex(RuntimeError,'시작 안전자세가 변경'):
            load.execute(self.episodes['arm3'],self.bundles['arm3'],300,1.5)
        self.assertNotIn(('arm3','play'),self.trace)
    def test_peer_move_failure_cannot_start_build(self):
        original=self.links['arm3'].rpc
        def fail(method,args=None):
            if method=='command' and args['action']=='move':raise RuntimeError('peer failed')
            return original(method,args)
        with patch.object(self.links['arm3'],'rpc',side_effect=fail),self.assertRaisesRegex(RuntimeError,'peer failed'):self.build()
        self.assertFalse(any(action=='play' for _,action in self.trace))
        self.assertIn(('arm2','hold'),self.trace);self.assertIn(('arm3','hold'),self.trace)
    def test_torque_off_after_pair_preparation_cannot_reuse_start_receipt(self):
        self.build();self.links['arm3'].mode='READ_ONLY';before=len(self.trace)
        load=self.runner('arm3');load.prepared_start=self.shared['receipts']['arm3']
        with self.assertRaisesRegex(RuntimeError,'시작 안전자세가 변경'):
            load.execute(self.episodes['arm3'],self.bundles['arm3'],300,1.5)
        self.assertFalse(any(action in ('arm','move','play') for _,action in self.trace[before:]))
