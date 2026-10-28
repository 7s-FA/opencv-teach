"""Persistent pure geometry retains request isolation, cancellation and faults."""
import threading,time,unittest
from unittest.mock import patch
from copy import deepcopy
from tests.fixtures import DATA,load_profile
from so101_teach.geometry import Kinematics
from so101_teach.plan_worker import PlanWorker,calculate_plan
from tests import test_remote as remote

class PlanningWorkerTests(unittest.TestCase):
    def inputs(self):
        profile,cal,ref=load_profile(DATA);kin=Kinematics(ref,tcp=profile['tcp'])
        return kin,[{'ticks':dict(ref.middle),'jig_id':None}],{}, {},cal.sha256,'bundle'
    def test_reuses_child_and_cannot_leak_previous_inputs(self):
        worker=PlanWorker();self.addCleanup(worker.close);args=self.inputs()
        expected=calculate_plan(deepcopy(args));self.assertEqual(worker.calculate(args),expected)
        process=worker.process;args[1][0]['ticks']['gripper']+=1
        result=worker.calculate(args);self.assertIs(worker.process,process)
        self.assertEqual(result,calculate_plan(deepcopy(args)));self.assertNotEqual(result,expected)
    def test_failure_discards_child_and_only_new_request_restarts(self):
        worker=PlanWorker();self.addCleanup(worker.close);args=self.inputs();bad=deepcopy(args);bad[1][0]['ticks']={}
        with self.assertRaisesRegex(RuntimeError,'경로 계산 실패'):worker.calculate(bad)
        self.assertIsNone(worker.process)
        self.assertEqual(worker.calculate(args),calculate_plan(args))
    def test_timeout_discards_process_and_close_prohibits_restart(self):
        worker=PlanWorker();self.addCleanup(worker.close)
        with self.assertRaisesRegex(RuntimeError,'시간 초과'):worker.calculate(self.inputs(),timeout=.00001)
        self.assertIsNone(worker.process);worker.close()
        with self.assertRaisesRegex(RuntimeError,'종료'):worker.calculate(self.inputs())
    def test_dead_worker_is_not_replayed(self):
        worker=PlanWorker();self.addCleanup(worker.close);args=self.inputs();worker.calculate(args)
        worker.process.kill();worker.process.wait()
        with self.assertRaises((RuntimeError,BrokenPipeError)):worker.calculate(args)
        self.assertIsNone(worker.process)
    def test_close_does_not_wait_for_calculation_lock(self):
        worker=PlanWorker();worker.lock.acquire()
        try:
            start=time.monotonic();worker.close();self.assertLess(time.monotonic()-start,.2)
        finally:worker.lock.release()

class PlanStopGenerationTests(unittest.TestCase):
    setUp=remote.RemoteTests.setUp
    tearDown=remote.RemoteTests.tearDown
    rpc=remote.RemoteTests.rpc
    def test_stop_while_planning_rejects_old_result(self):
        step=self.runtime.store.step(self.runtime.ref.middle,'fixed');original=self.runtime.calculate_plan
        def stopped(inputs):
            result=original(inputs);self.runtime.stop_generation+=1;return result
        with patch.object(self.runtime,'calculate_plan',side_effect=stopped):result=self.rpc('plan_snapshot',{'steps':[step],'current':{}})
        self.assertFalse(result['ok']);self.assertIn('정지 상태',result['error'])
