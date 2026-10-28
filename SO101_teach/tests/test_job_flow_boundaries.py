"""Whole-job ownership/exit behavior without opening hardware or ROS."""
import fcntl
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tests.test_episode_cli import cli
from tests.standby_fixtures import PreparedResources
import standby_preparation
import episode_recovery as recovery
import linear_client
from so101_teach import pc_compute,episode_inspection_runtime

SCENARIOS=[(c,None) for c in cli.JOB_PLANS]+[
    ('build_load_a',p) for p in ('load','open_arm3','linear_open','execute_arm2','execute_arm3','reset','reset_failure','linear_close')]

def exercise_job(function,command,fault=None):
    trace=[];counter=[0]
    def record(*args):trace.append(args)
    def fail(point):
        if fault==point:raise RuntimeError(point)
    class Thread:
        def __init__(self,*a,**k):pass
        def start(self):record('thread.start')
        def join(self,n):record('thread.join',n)
    class Control(cli.Control):
        def __init__(self):super().__init__();self.job_id='a'*32
        def begin_ending(self,connections):record('ending',tuple(x.arm if x else None for x in connections))
    class Link:
        def __init__(self,arm):self.arm=arm;self.lease=None
        def open(self):record('open',self.arm);fail('open_'+self.arm);self.lease='owned'
        def close(self):record('close',self.arm);self.lease=None
        def rpc(self,method,args=None):record('rpc',self.arm,method,args)
    class Linear:
        def open(self):record('linear.open');fail('linear_open')
        def close(self):record('linear.close');fail('linear_close')
    class Inspector:
        def __init__(self,*a,**k):record('inspector.create')
        def close(self):record('inspector.close')
    class Analysis:
        def context(self,*a):record('context',a[0])
        def resource_prepared(self,*a):record('prepared',*a)
        def cancel(self):record('analysis.cancel')
        def error(self,exc,phase):record('analysis.error',str(exc),phase)
    class Status:
        def __init__(self,arm,letter,ros,**kw):
            counter[0]+=1;self.arm=arm;self.run_id=str(counter[0]);self.publish_latest=kw.get('publish_latest',True)
            self.timing=SimpleNamespace(begin=lambda *a,**k:None);self.analysis=Analysis();record('status.create',arm,kw.get('status_fd'))
        def configure_analysis(self,*a,**k):pass
        def emit(self,event):record('emit',self.arm,event)
        def handoff(self,arm,letter):record('handoff',arm);return Status(arm,letter,False)
        def flush_final(self):record('status.flush',self.arm)
        def close(self):record('status.close',self.arm)
    class Runner:
        def __init__(self,link,emit,control,timeout,**kw):
            self.link=link;self.emit=emit;self.chain_start=None;self.on_work_started=None
            self.failure_context={'phase':'motion'};self.safe_ticks={};self.failure_inspection=None
        def check(self):record('runner.check')
        def prepare_linear(self,target):record('linear.prepare',target)
        def emergency(self):record('hold',self.link.arm if self.link else None);return []
        def save_failure_images(self,exc):record('failure.images',str(exc))
        def execute(self,episode,bundle,speed,target):
            # The same job lock must remain held through the build/load handoff.
            with (cli.ROOT/'data/episode-cli.lock').open('a') as other:
                try:fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:record('lock.held',self.link.arm)
                else:raise AssertionError('job lock released before phase completed')
            record('execute',self.link.arm,target)
            if self.chain_start is not None:self.chain_start['receipts']={'arm2':{'request_id':1},'arm3':{'request_id':2}}
            if self.on_work_started:self.on_work_started()
            if fault in ('reset','reset_failure') and self.link.arm=='arm2':raise cli.ResetRequested('RESET')
            fail('execute_'+self.link.arm)
            # The real runner owns a transferred inspector until execute returns.
            if getattr(self,'prepared_inspector',None):self.prepared_inspector.close();self.prepared_inspector=None
    def load(arm,letter,**kw):
        record('load',arm,letter);fail('load')
        episode={'id':arm,'name':arm,'steps':[{'id':'start','safe_boundary':'start','ticks':dict.fromkeys('abcdef',2048)},
            {'id':'placed','inspection':{'station':'finished','target':'pallet','expected':'cap_added'}},
            {'id':'end','safe_boundary':'end','ticks':dict.fromkeys('abcdef',2048)}]}
        return cli.ROOT/'data',episode,{'profile':{'robot_id':arm}},300,120
    def reset(*a,**kw):record('reset');fail('reset_failure')
    compute=SimpleNamespace(metrics=[],close=lambda:record('compute.close'))
    with TemporaryDirectory() as folder,ExitStack() as stack:
        root=Path(folder);(root/'data').mkdir()
        replacements=[(standby_preparation,'WorkcellPreparation',PreparedResources),(cli,'ROOT',root),(cli,'Control',Control),(cli,'Link',Link),(cli,'Runner',Runner),(cli,'Status',Status),
            (cli,'load_job',load),(cli,'open_links',lambda *links:[l.open() for l in links]),
            (cli,'finish_controls',lambda *a:record('controls.finish')),(cli,'atomic_json',lambda *a:record('compute.metrics')),
            (cli.threading,'Thread',Thread),(cli.signal,'signal',lambda *a:None),
            (linear_client,'LinearClient',Linear),(episode_inspection_runtime,'PreparedInspector',Inspector),
            (pc_compute,'session_for',lambda *a:compute),(pc_compute,'start_guard',lambda *a:Thread()),(pc_compute,'validate_profile',lambda *a:None),
            (recovery,'invalidate',lambda root,arm:record('invalidate',arm)),
            (recovery,'save_ready',lambda root,arm,*a:record('safe.save',arm)),
            (recovery,'save_failure',lambda root,arm,*a:record('failure.save',arm)),
            (recovery,'workcell_records',lambda *a:{}),(recovery,'reset_workcell',reset)]
        for owner,name,value in replacements:stack.enter_context(patch.object(owner,name,value))
        result=function(command,cli.parse_command([command,'--no-ros']))
    return {'result':result,'trace':trace}

class JobFlowBoundaryTests(TestCase):
    def test_chain_holds_ownership_until_both_phases_and_finishes_once(self):
        r=exercise_job(cli.run_job,'build_load_a');self.assertEqual(r['result'],0)
        t=r['trace'];self.assertIn(('lock.held','arm2'),t);self.assertIn(('lock.held','arm3'),t)
        end=t.index(('emit','arm3','CHAIN_DONE'))
        self.assertGreater(t.index(('close','arm2')),end);self.assertGreater(t.index(('close','arm3')),end)
        self.assertEqual(t.count(('inspector.close',)),1)
        self.assertLess(t.index(('controls.finish',)),t.index(('linear.close',)))
        self.assertGreater(t.index(('status.flush','arm3')),t.index(('close','arm2')))
    def test_partial_preparation_and_phase_failures_never_advance_to_next_work(self):
        for point in ('load','open_arm3','linear_open','execute_arm2','execute_arm3'):
            with self.subTest(point=point):
                r=exercise_job(cli.run_job,'build_load_b',point);self.assertEqual(r['result'],1)
                if point!='execute_arm3':self.assertNotIn(('execute','arm3',1.5),r['trace'])
                self.assertNotIn(('emit','arm3','CHAIN_DONE'),r['trace'])
                self.assertEqual(r['trace'].count(('controls.finish',)),1)
    def test_reset_result_and_reset_failure_remain_distinct(self):
        self.assertEqual(exercise_job(cli.run_job,'build_load_a','reset')['result'],130)
        r=exercise_job(cli.run_job,'build_load_a','reset_failure');self.assertEqual(r['result'],1)
        self.assertIn(('hold','arm2'),r['trace']);self.assertNotIn(('execute','arm3',1.5),r['trace'])
    def test_linear_close_error_does_not_skip_link_and_status_cleanup(self):
        r=exercise_job(cli.run_job,'build_a','linear_close')
        self.assertEqual(r['result'],0)
        self.assertIn(('close','arm2'),r['trace']);self.assertIn(('close','arm3'),r['trace'])
        self.assertEqual(r['trace'][-1],('status.close','arm2'))
