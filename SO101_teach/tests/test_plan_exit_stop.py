"""Control stays live while failed preparation drains read-only planning."""
import json,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink,FakeLinear

class PlanExitStopTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        p=patch.object(cli,'ROOT',Path(tmp.name));p.start();self.addCleanup(p.stop)
        self.release=threading.Event();self.entered=threading.Event();self.failed=threading.Event();self.finished=threading.Event()
        self.camera_entered=threading.Event();self.camera_release=threading.Event()
        self.control=cli.Control();self.link=FakeLink();self.link.mode='HOLD';self.calls=0;self.errors=[]
        self.runner=cli.Runner(self.link,lambda e:None,self.control,30,linear=FakeLinear())
        original=self.link.rpc
        def rpc(method,args=None):
            if method=='plan_snapshot':
                self.calls+=1;self.entered.set()
                if not self.release.wait(15):raise TimeoutError('test solver timeout')
                return {'plan':[]}
            if method=='camera_stop' and self.runner.camera_owned:
                self.camera_entered.set()
                if not self.camera_release.wait(15):raise TimeoutError('test camera cleanup timeout')
            return original(method,args)
        self.link.rpc=rpc
    def start(self,*,queued=False,error=None,camera_cleanup=False):
        def prepare(*args,**kw):
            kw['on_measured']({});self.assertTrue(self.entered.wait(1))
            if queued:kw['on_measured']({'new':True})
            self.failed.set();raise error or RuntimeError('inspection failed')
        self.runner.prepare_work=prepare
        if camera_cleanup:
            self.runner.camera_owned=True
            self.runner._execute=lambda *args:self.runner.prepare_execution_plan([],{},100.,[])
        def job():
            try:
                if camera_cleanup:self.runner.execute({'steps':[]},{},400,100.)
                else:self.runner.prepare_execution_plan([],{},100.,[])
            except BaseException as exc:self.errors.append(exc)
            finally:cli.finish_controls(self.control,[self.link]);self.finished.set()
        self.thread=threading.Thread(target=job);self.thread.start()
        self.addCleanup(self.close)
        self.assertTrue(self.failed.wait(1))
    def close(self):
        self.release.set();self.camera_release.set();self.thread.join(4);self.assertFalse(self.thread.is_alive())
        self.assertIsNone(self.control.ending_worker)
    def request(self,key='a',command='estop'):
        c=self.control
        return json.loads(c.command('control '+json.dumps(dict(job_id=c.job_id,request_id=key*32,command=command,stop_generation=c.stop_generation))))
    def until(self,predicate,seconds=1):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:time.sleep(.01)
        self.assertTrue(predicate())
    def test_stop_completes_while_solver_still_pending_and_failed_job_cannot_resume(self):
        self.start();self.until(lambda:self.control.ending)
        for key in ('a','b'):
            self.assertEqual(self.request(key)['state'],'pending')
            self.until(lambda:self.control.receipts[key*32]['state']=='completed')
            self.assertFalse(self.finished.is_set());self.assertFalse(self.release.is_set())
        for command in ('restart','reset'):
            with self.assertRaisesRegex(ValueError,'JOB_ENDING'):self.request('c',command)
        self.close();self.assertEqual(str(self.errors[0]),'inspection failed')
        self.assertTrue(all(a.get('action')=='hold' for m,a in self.link.calls if m=='command'))
    def test_cancelled_queued_plan_does_not_hide_older_running_plan(self):
        self.start(queued=True);self.request()
        self.until(lambda:self.control.receipts['a'*32]['state']=='completed')
        self.assertFalse(self.finished.is_set());self.close();self.assertEqual(self.calls,1)
    def test_stale_feedback_fails_at_existing_stop_timeout_not_solver_timeout(self):
        self.start();self.link.stale=True;began=time.monotonic();self.request()
        self.until(lambda:self.control.receipts['a'*32]['state']=='failed',seconds=4)
        self.assertLess(time.monotonic()-began,4);self.assertFalse(self.finished.is_set())
    def test_reset_unwind_is_not_reclassified_as_a_failed_job(self):
        self.start(error=cli.ResetRequested('RESET'));self.request()
        self.until(lambda:self.control.receipts['a'*32]['state']=='completed')
        self.assertFalse(self.control.ending);self.close();self.assertIsInstance(self.errors[0],cli.ResetRequested)
    def test_stop_received_before_failure_also_completes_without_waiting_for_solver(self):
        self.request();self.start()
        self.until(lambda:self.control.receipts['a'*32]['state']=='completed')
        self.assertFalse(self.finished.is_set())

    def test_repeated_stops_complete_after_solver_exit_during_camera_cleanup(self):
        self.start(camera_cleanup=True);self.request('a')
        self.until(lambda:self.control.receipts['a'*32]['state']=='completed')
        self.release.set();self.assertTrue(self.camera_entered.wait(1))
        for key in ('b','c','d'):
            self.request(key);self.until(lambda:self.control.receipts[key*32]['state']=='completed')
            self.until(lambda:self.control.ending_worker is None)
            self.assertFalse(self.finished.is_set());self.assertFalse(self.camera_release.is_set())
        for command in ('restart','reset'):
            with self.assertRaisesRegex(ValueError,'JOB_ENDING'):self.request('e',command)
        self.close();self.assertEqual(str(self.errors[0]),'inspection failed')
        self.assertFalse(self.control.resume.is_set());self.assertFalse(self.control.reset.is_set())
        self.assertTrue(all(a.get('action')=='hold' for m,a in self.link.calls if m=='command'))

    def test_stale_peer_fails_during_camera_cleanup_without_waiting_for_camera(self):
        self.runner.peer=FakeLink();self.runner.peer.stale=True
        self.start(camera_cleanup=True);self.release.set();self.assertTrue(self.camera_entered.wait(1))
        began=time.monotonic();self.request()
        self.until(lambda:self.control.receipts['a'*32]['state']=='failed',seconds=4)
        self.assertLess(time.monotonic()-began,4);self.assertFalse(self.finished.is_set())
        self.assertFalse(self.camera_release.is_set())

    def test_finalizer_seals_requests_then_joins_the_confirmation_owner(self):
        entered=threading.Event();release=threading.Event();finished=threading.Event()
        c=self.control;c.begin_ending([self.link]);original=c.confirm_estop
        def confirm(states):entered.set();self.assertTrue(release.wait(2));return original(states)
        with patch.object(c,'confirm_estop',side_effect=confirm):
            self.request();self.assertTrue(entered.wait(1))
            worker=c.ending_worker
            t=threading.Thread(target=lambda:(cli.finish_controls(c,[self.link]),finished.set()));t.start()
            try:
                self.until(c.shutdown.is_set);self.assertFalse(finished.is_set())
                with self.assertRaisesRegex(ValueError,'JOB_ENDING'):self.request('b')
            finally:release.set();t.join(2)
        self.assertTrue(finished.is_set());self.assertFalse(worker.is_alive())
        self.assertIsNone(c.ending_worker);self.assertEqual(c.receipts['a'*32]['state'],'completed')
        self.assertEqual(self.request()['state'],'completed')

    def test_repeat_arriving_during_completion_is_not_lost_at_worker_exit(self):
        c=self.control;c.begin_ending([self.link]);original=c.save_receipts;repeated=[]
        def save():
            if not repeated and c.receipts['a'*32]['state']=='completed':
                repeated.append(True);self.request('b')
            original()
        with patch.object(c,'save_receipts',side_effect=save):
            self.request();self.until(lambda:c.receipts.get('b'*32,{}).get('state')=='completed')
            cli.finish_controls(c,[self.link])
        self.assertEqual([r['state'] for r in c.receipts.values()],['completed','completed'])
        self.assertIsNone(c.ending_worker)

    def test_startup_failure_serves_stop_before_inspection_cleanup_returns(self):
        entered=threading.Event();release=threading.Event();errors=[]
        def close_pipeline():entered.set();self.assertTrue(release.wait(3))
        pipeline=SimpleNamespace(observed={},close=close_pipeline)
        runner=self.runner;runner.inspection_records=[];runner.inspection_log=cli.ROOT/'inspection.json'
        runner.inspection_episode='test';runner.inspection_product='A'
        def job():
            try:runner.prepare_startup([],{'profile':{}},[])
            except BaseException as exc:errors.append(str(exc))
            finally:cli.finish_controls(self.control,[self.link])
        with patch('so101_teach.configuration.JigCatalog'),patch('so101_teach.workcell_placement.load_placement',return_value={}),patch('so101_teach.startup_inspection.StartupInspection',return_value=pipeline),patch.object(runner,'measure',side_effect=RuntimeError('inspection failed')):
            t=threading.Thread(target=job);t.start()
            try:
                self.assertTrue(entered.wait(1));self.assertTrue(self.control.ending)
                self.request();self.until(lambda:self.control.receipts['a'*32]['state']=='completed')
                self.assertFalse(release.is_set())
            finally:release.set();t.join(2)
        self.assertFalse(t.is_alive());self.assertEqual(errors,['inspection failed'])
        self.assertIsNone(self.control.ending_worker)

    def test_same_stop_retry_after_receipt_write_failure_starts_confirmation(self):
        c=self.control;c.begin_ending([self.link])
        with patch.object(c,'save_receipts',side_effect=OSError('temporary write failure')):
            with self.assertRaises(OSError):self.request()
        self.assertEqual(c.receipts['a'*32]['state'],'pending');self.assertIsNone(c.ending_worker)
        self.request();self.until(lambda:c.receipts['a'*32]['state']=='completed')
        cli.finish_controls(c,[self.link]);self.assertIsNone(c.ending_worker)

    def test_early_shutdown_keeps_previously_accepted_stop_confirmation_alive(self):
        c=self.control;self.request();c.shutdown.set()
        try:
            c.begin_ending([self.link])
            self.until(lambda:c.receipts['a'*32]['state']=='completed')
            with self.assertRaisesRegex(ValueError,'JOB_ENDING'):self.request('b')
        finally:cli.finish_controls(c,[self.link])
        self.assertIsNone(c.ending_worker)
