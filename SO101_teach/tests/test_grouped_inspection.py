import unittest,time,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
from copy import deepcopy
from so101_teach.episode_inspection import GroupInspectionDecision,validate_check
from tests.test_episode_inspection import CHECK,result

class GroupTests(unittest.TestCase):
    def steps(self,minimum=2):return [{'id':key,'name':key,'inspection':{**CHECK,'minimum_observations':minimum},'inspection_product':'A'} for key in ('one','two')]
    def test_parts_share_frame_but_counts_are_independent_and_missing_does_not_count(self):
        d=GroupInspectionDecision(self.steps(),10)
        self.assertFalse(d.observe({'results':{'one':result(10.1),'two':{'at':10.1,'row':None}}},10.2))
        passed=d.observe({'results':{'one':result(10.3),'two':result(10.3)}},10.4)
        self.assertEqual([s['id'] for s in passed],['one']);self.assertEqual(d.pending['two'].count,1)
        d.observe({'results':{'two':{'at':10.5,'row':None}}},10.6);self.assertEqual(d.pending['two'].count,1)
        self.assertEqual([s['id'] for s in d.observe({'results':{'two':result(10.7)}},10.8)],['two'])
    def test_minimum_three_needs_three_distinct_valid_results(self):
        d=GroupInspectionDecision(self.steps(3),10)
        for at in (10.1,10.3):self.assertFalse(d.observe({'results':{'one':result(at),'two':result(at)}},at+.01))
        self.assertFalse(d.observe({'results':{'one':result(10.3),'two':result(10.3)}},10.4))
        self.assertEqual(len(d.observe({'results':{'one':result(10.5),'two':result(10.5)}},10.6)),2)
    def test_unavailable_cannot_become_pass_and_timeout_names_part(self):
        d=GroupInspectionDecision(self.steps(),10)
        for at in (10.1,10.3,10.5):d.observe({'results':{'one':result(at,product_certain=False)}},at+.01)
        self.assertEqual(d.pending['one'].count,0)
        with self.assertRaisesRegex(ValueError,'one.*시간 초과'):d.observe(None,16)
    def test_bad_result_stops_after_minimum_count(self):
        d=GroupInspectionDecision(self.steps(),10)
        d.observe({'results':{'two':result(10.1,quality='abnormal')}},10.2)
        with self.assertRaisesRegex(ValueError,'two.*불합격'):d.observe({'results':{'two':result(10.3,quality='abnormal')}},10.4)
    def test_per_part_minimum_is_validated(self):
        validate_check(CHECK)
        for value in (1,11,2.5,True):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_check({**CHECK,'minimum_observations':value})
    def test_worker_localizes_once_and_classifies_all_pending_targets_together(self):
        import numpy as np
        from so101_teach.episode_inspection_runtime import FrameInspections
        steps=self.steps();steps[1]['inspection']['target']='2'
        anchors=[{'id':'linear_0','inspection_allowed':True},{'id':'linear_1','inspection_allowed':True}]
        detector=Mock();detector.process.return_value={'live_by_jig':{}}
        inspector=Mock();inspector.inspect.return_value=[{'id':a['id'],'quality':'normal'} for a in anchors]
        catalog=SimpleNamespace(items={},mesh=lambda key:{})
        with tempfile.TemporaryDirectory() as temp,patch('so101_teach.vision_service.MultiDetector',return_value=detector),patch('so101_teach.shape_inspection.ShapeInspector',return_value=inspector),patch('so101_teach.inspection_geometry.anchors_for',return_value=(anchors,'test')):
            w=FrameInspections(temp,{},catalog,{'linear_stage':{'startup_state':{'known':True}}},steps)
            try:
                w.submit(np.zeros((4,4,3),dtype=np.uint8),time.monotonic());until=time.monotonic()+2
                while w.result is None and time.monotonic()<until:time.sleep(.01)
                self.assertIn('results',w.result);self.assertEqual(set(w.result['results']),{'one','two'})
                detector.process.assert_called_once();self.assertEqual(len(inspector.inspect.call_args.args[1]),2)
                w.retain(['two']);w.result=None;w.submit(np.zeros((4,4,3),dtype=np.uint8),time.monotonic());until=time.monotonic()+2
                while w.result is None and time.monotonic()<until:time.sleep(.01)
                self.assertEqual(set(w.result['results']),{'two'});self.assertEqual(len(inspector.inspect.call_args.args[1]),1)
            finally:w.close()

class PiGroupTests(unittest.TestCase):
    def test_real_group_loop_passes_all_targets_and_reports_counts_without_motion(self):
        from tests.test_episode_cli import cli,FakeLink,FakeLinear
        steps=GroupTests().steps();clock=[100.];link=FakeLink();events=[]
        class Worker:
            def __init__(self,*args,**kwargs):self.ids={s['id'] for s in steps}
            @property
            def result(self):return {'results':{key:result(clock[0]-.01) for key in self.ids}}
            def retain(self,ids):self.ids=set(ids)
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(cli.time,'monotonic',side_effect=lambda:clock[0]),patch.object(cli.time,'sleep',side_effect=lambda t:clock.__setitem__(0,clock[0]+t)),patch('so101_teach.configuration.JigCatalog'),patch('so101_teach.workcell_preview.load_placement',return_value={'linear_stage':{}}),patch('so101_teach.episode_inspection_runtime.FrameInspections',Worker):
            runner=cli.Runner(link,events.append,cli.Control(),20,linear=FakeLinear());runner.inspection_episode='e';runner.inspection_product='A';runner.inspection_records=[];runner.inspection_log=Path(folder)/'result.json'
            runner.inspect_startup_group(steps,{'profile':{}})
        self.assertIn('INSPECTION_GROUP_STARTED:2',events);self.assertIn('INSPECTION_PASS:one',events);self.assertIn('INSPECTION_PASS:two',events)
        self.assertTrue(any('INSPECTION_COUNT:one:1/2' in e for e in events));self.assertFalse(any(m=='command' for m,a in link.calls))
    def test_preparation_remeasures_after_resume_inside_group(self):
        from tests.test_episode_cli import cli,FakeLink,FakeLinear
        runner=cli.Runner(FakeLink(),lambda e:None,cli.Control(),20,linear=FakeLinear())
        with patch.object(runner,'measure',return_value={}) as measure,patch.object(runner,'inspect_startup_group',side_effect=[True,False]) as inspect:
            runner.prepare_work([],{},100,[{'inspection':{'station':'carrier'}}])
        self.assertEqual(measure.call_count,2);self.assertEqual(inspect.call_count,2)
