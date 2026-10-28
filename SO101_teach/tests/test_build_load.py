import tempfile,unittest,fcntl
from pathlib import Path
from unittest.mock import patch,Mock
from tests.test_episode_cli import cli
from tests.standby_fixtures import PreparedResources
import standby_preparation
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
            def rpc(self,*args):events.append(('rpc',self.arm,*args))
        class Linear:
            def open(self):pass
            def close(self):pass
        class Runner:
            def __init__(self,link,emit,control,timeout,**kw):self.link=link;self.emit=emit;self.safe_ticks={};self.failure_context={};self.failure_inspection=None;self.finished_return_step=None;self.phase=len([e for e in events if e[0]=='execute']);self.assembly_evidence=SimpleNamespace(receipt={'episode_id':'current-build','completed_at':123.})
            def execute(self,ep,bundle,speed,target):
                with (root/'data/episode-cli.lock').open('a') as lock:
                    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:pass
                    else:raise AssertionError('combined workcell lock was released')
                if getattr(self,'chain_start',None):
                    events.append(('pair_start',tuple(self.chain_start['records'])))
                    if fail=='pair_start':raise RuntimeError('pair start failed')
                    self.chain_start['receipts']={a:{'request_id':1,'ticks':r['safe_ticks']} for a,r in self.chain_start['records'].items()}
                if getattr(self,'prepared_start',None):events.append(('prepared_start',self.link.arm,self.prepared_start))
                if getattr(self,'on_work_started',None):self.on_work_started()
                if getattr(self,'prepared_inspector',None):events.append(('prepared_inspector',self.link.arm,self.prepared_inspector))
                events.append(('execute',self.link.arm,target))
                if self.link.arm=='arm3':events.append(('history_required',hasattr(self,'chain_handoff') or hasattr(self,'assembly_handoff')))
                if fail==self.link.arm:raise RuntimeError('inspection failed')
                if fail=='reset':raise cli.ResetRequested('reset')
                events.append(('return_trigger',self.link.arm,self.finished_return_step))
                self.emit('DONE')
            def check(self):
                if fail=='boundary':raise cli.ResetRequested('reset between phases')
            def reset_to_safe(self):events.append(('reset',self.link.arm))
            def emergency(self):events.append(('hold',self.link.arm))
            def save_failure_images(self,error):events.append(('failure_images',self.link.arm,str(error)))
        def load(arm,letter,**kwargs):
            events.append(('validate',arm,letter))
            if fail=='load_validation' and arm=='arm3':raise ValueError('invalid load')
            check={'station':'finished','target':'pallet','expected':'cap_added','timeout_seconds':10}
            ep=dict(id='e',name=letter,product_type=letter,calibration_sha256='cal',steps=[dict(id='start',ticks={str(i):2048 for i in range(6)},name='safe start',safe_boundary='start'),dict(id='placed',ticks={},name='placed',inspection=check),dict(id='s',ticks={},name='safe',safe_boundary='end')])
            if fail=='missing_seating' and arm=='arm3':ep['steps'][1].pop('inspection')
            if fail=='missing_safe' and arm=='arm3':ep['steps'][0].pop('safe_boundary')
            return root,ep,{'profile':{'robot_id':arm}},300,30
        def reset_all(api,records,links,linear,control,emit,**kw):
            events.append(('reset',kw['first']));events.append(('reset','arm3'));emit('RESET_DONE')
        self.prewarmed=Mock()
        with patch.object(standby_preparation,'WorkcellPreparation',PreparedResources),patch('so101_teach.episode_inspection_runtime.PreparedInspector',return_value=self.prewarmed),patch.object(episode_recovery,'workcell_records',return_value={}),patch.object(episode_recovery,'reset_workcell',side_effect=reset_all),patch.object(cli,'ROOT',root),patch.object(cli,'load_job',side_effect=load),patch.object(cli,'Link',Link),patch.object(cli,'Runner',Runner),patch.object(linear_client,'LinearClient',Linear),patch.object(cli,'control_server',side_effect=lambda control,done:done.wait(2)),patch.object(cli.signal,'signal'):
            return cli.main([command,'--no-ros']+(['--check'] if check else []))
    def test_build_finishes_before_load_and_lock_is_retained(self):
        self.assertEqual(self.run_command(),0)
        self.assertEqual([e for e in self.events if e[0]=='execute'],[('execute','arm2',100.),('execute','arm3',1.5)])
        self.assertEqual(self.events[:2],[('validate','arm2','B'),('validate','arm3','B')])
        self.assertIn(('history_required',False),self.events)
        self.assertIn(('return_trigger','arm2',None),self.events)
        self.assertIn(('return_trigger','arm3','placed'),self.events)
        self.assertIn(('prepared_inspector','arm3',self.prewarmed),self.events)
    def test_product_a_load_also_receives_this_run_seating_trigger(self):
        self.assertEqual(self.run_command(command='build_load_a'),0)
        self.assertIn(('return_trigger','arm3','placed'),self.events)
        self.assertEqual(sum(e[0]=='pair_start' for e in self.events),1)
        self.assertTrue(any(e[0]=='prepared_start' and e[1]=='arm3' for e in self.events))
    def test_missing_peer_start_pose_is_rejected_before_devices_open(self):
        self.assertEqual(self.run_command(fail='missing_safe'),1)
        self.assertFalse(any(e[0]=='open' for e in self.events))
    def test_failed_pair_preparation_never_starts_either_episode(self):
        self.assertEqual(self.run_command(fail='pair_start'),1)
        self.assertFalse(any(e[0]=='execute' for e in self.events))
    def test_missing_full_product_seating_check_rejected_before_connection(self):
        self.assertEqual(self.run_command(fail='missing_seating'),1)
        self.assertFalse(any(e[0]=='open' for e in self.events))
    def test_load_failure_does_not_schedule_successful_idle_release(self):
        self.assertEqual(self.run_command(fail='arm3'),1)
        self.assertFalse(any(e[0]=='rpc' and e[2]=='schedule_idle_release' for e in self.events))
        self.assertFalse(any(e[0]=='return_trigger' and e[1]=='arm3' for e in self.events))
    def test_independent_load_has_no_linear_return_trigger(self):
        self.assertEqual(self.run_command(command='load_a'),0)
        self.assertIn(('return_trigger','arm3',None),self.events)
    def test_build_failure_never_starts_load(self):
        self.assertEqual(self.run_command(fail='arm2'),1)
        self.assertEqual([e[1] for e in self.events if e[0]=='execute'],['arm2'])
        self.prewarmed.close.assert_called_once()
    def test_reset_at_handoff_cancels_remaining_load(self):
        self.assertEqual(self.run_command(fail='boundary'),130)
        self.assertEqual([e[1] for e in self.events if e[0]=='execute'],['arm2']);self.assertIn(('reset','arm2'),self.events)
    def test_bad_load_is_rejected_before_any_device_connection(self):
        self.assertEqual(self.run_command(fail='load_validation'),1);self.assertFalse(any(e[0]=='open' for e in self.events))
    def test_check_validates_both_products_without_devices(self):
        for command in ('build_load_a','build_load_b'):
            self.events=[];self.assertEqual(self.run_command(command=command,check=True),0)
            self.assertEqual(len(self.events),2);self.assertTrue(all(e[0]=='validate' for e in self.events))
