"""Preparation overlaps pure IK, but never weakens the gates before motion."""
from copy import deepcopy
import threading
import unittest
import uuid
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear
from tests import test_remote as remote


class SnapshotPlanTests(unittest.TestCase):
    setUp=remote.RemoteTests.setUp
    tearDown=remote.RemoteTests.tearDown
    rpc=remote.RemoteTests.rpc

    def request(self):
        step=self.runtime.store.step(self.runtime.ref.middle,'fixed')
        return {'lease':self.lease,'id':uuid.uuid4().hex,'method':'plan_snapshot','args':{'steps':[step],'current':{}}}

    def test_same_solver_and_input_validation_as_serial_plan(self):
        body=self.request();old=self.rpc('plan',body['args']);new=self.runtime.rpc(body)
        self.assertEqual(new,old)
        bad=deepcopy(body);bad['id']=uuid.uuid4().hex;bad['args']['steps'][0]['ticks']={}
        self.assertFalse(self.runtime.rpc(bad)['ok'])

    def test_corrected_snapshot_matches_existing_geometry_engine(self):
        original=self.rpc
        def snapshot(method,args=None):
            return original('plan_snapshot' if method=='plan' else method,args)
        with patch.object(self,'rpc',side_effect=snapshot):
            remote.RemoteTests.test_corrected_plan_matches_same_engine_locally(self)

    def test_camera_rpc_remains_available_while_solver_is_blocked(self):
        started=threading.Event();finish=threading.Event();values=[]
        original=self.runtime.calculate_plan
        def slow(inputs):
            started.set();self.assertTrue(finish.wait(2));return original(inputs)
        with patch.object(self.runtime,'calculate_plan',side_effect=slow):
            t=threading.Thread(target=lambda:values.append(self.runtime.rpc(self.request())))
            t.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(self.runtime.lock.acquire(timeout=.3))
                self.runtime.lock.release()
                self.assertTrue(self.rpc('camera_start',{'processing_enabled':False,'preview_fps':10})['ok'])
            finally:finish.set();t.join(2)
        self.assertFalse(t.is_alive());self.assertTrue(values[0]['ok'])

    def test_configuration_change_rejects_old_snapshot_without_mutating_it(self):
        started=threading.Event();finish=threading.Event();values=[];captured=[]
        original=self.runtime.calculate_plan
        def slow(inputs):
            captured.append(inputs);started.set();self.assertTrue(finish.wait(2));return original(inputs)
        with patch.object(self.runtime,'calculate_plan',side_effect=slow):
            t=threading.Thread(target=lambda:values.append(self.runtime.rpc(self.request())));t.start()
            try:
                self.assertTrue(started.wait(1));self.assertIsNot(captured[0][0],self.runtime.kin)
                changed=deepcopy(self.bundle);changed['jigs']['pallet']['roi']=[.1,.1,.8,.8]
                self.runtime.configure(changed)
            finally:finish.set();t.join(2)
        self.assertFalse(values[0]['ok']);self.assertIn('설정이 변경',values[0]['error'])

    def test_lease_change_rejects_a_finished_old_request(self):
        original=self.runtime.calculate_plan
        def replace_owner(inputs):
            value=original(inputs);self.runtime.detached=True;self.runtime.acquire();return value
        with patch.object(self.runtime,'calculate_plan',side_effect=replace_owner):reply=self.runtime.rpc(self.request())
        self.assertFalse(reply['ok'])


class PreparationPlanTests(unittest.TestCase):
    def setUp(self):
        self.link=FakeLink();self.events=[];self.control=cli.Control()
        self.run=cli.Runner(self.link,self.events.append,self.control,20,linear=FakeLinear())
        self.episode={'steps':[{'id':'safe','name':'safe','ticks':{},'safe_boundary':'start'}, {'id':'work','name':'work','ticks':{}}]}
        self.measured=[]
        def measure(*args):
            value={'j':{'pose':[len(self.measured)+1,0,0]}};self.measured.append(value);return value
        p=patch.object(self.run,'measure',side_effect=measure);p.start();self.addCleanup(p.stop)
        p=patch('so101_teach.episode_inspection.startup_steps',return_value=[{'inspection':{'station':'carrier'}}]);p.start();self.addCleanup(p.stop)
        # This suite isolates the planner from preparation. The real incremental
        # pipeline and its pause/linear gates are exercised in test_startup_pipeline.
        def prepare(steps,bundle,checks,*,on_measured=None):
            while True:
                current=self.run.measure(steps,bundle)
                if on_measured:on_measured(current)
                resumed=self.run.inspect_startup_group(checks,bundle)
                resumed=self.run.wait_linear() or resumed
                if not resumed:return current
        p=patch.object(self.run,'prepare_startup',side_effect=prepare);p.start();self.addCleanup(p.stop)
        self.rpc=self.link.rpc
    def plays(self):return [a for m,a in self.link.calls if m=='command' and a['action']=='play']
    def plans(self):return [a for m,a in self.link.calls if m=='plan_snapshot']

    def test_solver_overlaps_inspection_and_play_waits_for_inspection_and_linear(self):
        started=threading.Event();finish=threading.Event()
        def rpc(method,args=None):
            if method=='plan_snapshot':
                started.set();self.assertTrue(finish.wait(2))
            return self.rpc(method,args)
        def inspect(*args):
            self.assertTrue(started.wait(1));self.assertFalse(self.plays());finish.set()
            self.events.append('CHECK_PASSED');return False
        def linear():self.assertFalse(self.plays());self.events.append('LINEAR_PASSED');return False
        with patch.object(self.link,'rpc',side_effect=rpc),patch.object(self.run,'inspect_startup_group',side_effect=inspect),patch.object(self.run,'wait_linear',side_effect=linear):
            self.run.execute(self.episode,{},400,100.)
        for gate in ['CHECK_PASSED','LINEAR_PASSED']:
            self.assertLess(self.events.index('PLAN_STARTED'),self.events.index(gate))
            self.assertLess(self.events.index(gate),self.events.index('EPISODE_STARTED'))
        self.assertEqual(len(self.plays()),1)

    def test_inspection_failure_cannot_use_a_successful_plan(self):
        with patch.object(self.run,'inspect_startup_group',side_effect=ValueError('inspection failed')):
            with self.assertRaisesRegex(ValueError,'inspection failed'):self.run.execute(self.episode,{},400,100.)
        self.assertFalse(self.plays());self.assertNotIn('EPISODE_STARTED',self.events)

    def test_planning_failure_cannot_start_motion(self):
        def rpc(method,args=None):
            if method=='plan_snapshot':raise ValueError('IK failed')
            return self.rpc(method,args)
        with patch.object(self.link,'rpc',side_effect=rpc),patch.object(self.run,'inspect_startup_group',return_value=False):
            with self.assertRaisesRegex(ValueError,'IK failed'):self.run.execute(self.episode,{},400,100.)
        self.assertFalse(self.plays())

    def test_resume_during_inspection_remeasures_and_recalculates(self):
        count=[0]
        def inspect(*args):
            count[0]+=1
            if count[0]==1:self.run.preparation_epoch+=1;return True
            return False
        with patch.object(self.run,'inspect_startup_group',side_effect=inspect):self.run.execute(self.episode,{},400,100.)
        self.assertEqual(len(self.measured),2);self.assertEqual(self.plans()[-1]['current'],self.measured[-1]);self.assertEqual(len(self.plays()),1)

    def test_late_resume_before_play_restarts_all_preparation(self):
        move=self.run.move;paused=[False]
        def late_pause(action,*args,**kwargs):
            if action=='play' and not paused[0]:paused[0]=True;self.run.preparation_epoch+=1
            return move(action,*args,**kwargs)
        with patch.object(self.run,'move',side_effect=late_pause),patch.object(self.run,'inspect_startup_group',return_value=False):self.run.execute(self.episode,{},400,100.)
        self.assertEqual(len(self.measured),2);self.assertEqual(len(self.plays()),1)
        self.assertIn('PLAN_DISCARDED:PREPARATION_CHANGED',self.events)

    def test_changed_measured_coordinates_discard_old_plan(self):
        count=[0]
        def inspect(*args):
            count[0]+=1
            if count[0]==1:self.measured[-1]['j']['pose'][0]+=10
            return False
        with patch.object(self.run,'inspect_startup_group',side_effect=inspect):self.run.execute(self.episode,{},400,100.)
        self.assertEqual(len(self.measured),2);self.assertEqual(self.plans()[-1]['current'],self.measured[-1]);self.assertEqual(len(self.plays()),1)
