"""No device access: stale samples and accepted controls across job failure."""
import io,json,tempfile,time,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from tests.test_episode_cli import cli,FakeLink
from tests.test_work_monitor import monitor


class EndingControlTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        p=patch.object(cli,'ROOT',Path(temp.name));p.start();self.addCleanup(p.stop)
        self.control=cli.Control();self.key='a'*32
    def request(self):
        return json.loads(self.control.command('control '+json.dumps(dict(
            job_id=self.control.job_id,request_id=self.key,command='estop'))))
    def receipt(self):return self.control.receipts[self.key]['state']
    def test_failure_keeps_stop_pending_until_both_arms_confirm(self):
        self.request();status=cli.Status('arm2','A',False);status.control=self.control
        self.addCleanup(status.close)
        status.emit('FAILED:inspection failed');self.assertEqual(self.receipt(),'pending')
        links=[FakeLink(),FakeLink()]
        for link in links:link.mode='HOLD'
        cli.finish_controls(self.control,links)
        self.assertEqual(self.receipt(),'completed');self.assertTrue(self.control.shutdown.is_set())
        for link in links:self.assertEqual(link.calls,[('command',{'action':'hold'})])
        self.assertEqual(self.request()['state'],'completed')
        with self.assertRaisesRegex(ValueError,'JOB_ENDING'):self.control.command('estop')
    def test_delayed_telemetry_recovers_without_weakening_freshness(self):
        self.request();link=FakeLink();link.mode='HOLD';original=link.state;reads=[]
        def state():
            s=original();reads.append(s)
            if len(reads)<4:s['latest']['monotonic']-=.75
            return s
        link.state=state
        cli.finish_controls(self.control,[link],timeout=.5)
        self.assertGreaterEqual(len(reads),4);self.assertEqual(self.receipt(),'completed')
        self.assertFalse(cli.settled(reads[0]));self.assertTrue(cli.settled(reads[-1]))
    def test_stale_peer_never_completes_and_resume_never_runs(self):
        self.request();first=FakeLink();second=FakeLink();second.stale=True
        with self.assertRaisesRegex(RuntimeError,'시간 초과'):
            cli.finish_controls(self.control,[first,second],timeout=.06)
        self.assertEqual(self.receipt(),'failed');self.assertFalse(self.control.resume.is_set())
        self.assertFalse(any(a.get('action') in ('arm','move','play') for _,a in first.calls+second.calls))
    def test_still_moving_and_disconnected_and_error_are_not_confirmed(self):
        for variant in ('moving','disconnected','error','release'):
            self.control=cli.Control();self.request();link=FakeLink();link.mode='HOLD';original=link.state
            def state():
                s=original()
                if variant=='moving':s['latest']['telemetry']['0']['moving']=1
                if variant=='disconnected':s['running']=False
                if variant=='error':s['error']='lost bus'
                if variant=='release':s['release_pending']=True
                return s
            link.state=state
            with self.subTest(variant=variant),self.assertRaises(RuntimeError):cli.finish_controls(self.control,[link],timeout=.01)
            self.assertEqual(self.receipt(),'failed')
    def test_pre_request_fresh_sample_is_not_stop_confirmation(self):
        link=FakeLink();link.mode='HOLD';snapshot=link.state();link.state=lambda:snapshot
        self.request()
        with self.assertRaisesRegex(RuntimeError,'시간 초과'):cli.finish_controls(self.control,[link],timeout=.01)
        self.assertEqual(self.receipt(),'failed')
    def test_no_pending_stop_does_not_send_hold(self):
        link=FakeLink();cli.finish_controls(self.control,[link]);self.assertEqual(link.calls,[])
    def test_completed_record_write_failure_does_not_report_success(self):
        self.request();link=FakeLink()
        with patch.object(self.control,'save_receipts',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):cli.finish_controls(self.control,[link])
        self.assertNotEqual(self.receipt(),'completed')

    def test_torque_off_with_moving_feedback_never_completes(self):
        self.request();link=FakeLink();original=link.state
        def state():
            value=original();value['latest']['telemetry']['0']['moving']=1;return value
        link.state=state
        with self.assertRaisesRegex(RuntimeError,'시간 초과'):cli.finish_controls(self.control,[link],timeout=.01)
        self.assertEqual(self.receipt(),'failed');self.assertEqual(link.calls,[])

    def test_second_arm_delay_revalidates_first_sample_and_can_recover(self):
        for recover in (False,True):
            self.control=cli.Control();clock=[100.];observations={}
            class Arm(FakeLink):
                def __init__(self,name):super().__init__();self.mode='HOLD';self.name=name;self.reads=0
                def state(self):
                    self.reads+=1
                    if self.name=='arm3' and self.reads>=2 and (not recover or self.reads==2):clock[0]+=.7
                    value=super().state();observations[self.name]=value;return value
            links=[Arm('arm2'),Arm('arm3')]
            with patch.object(cli.time,'monotonic',lambda:clock[0]),patch.object(cli.time,'sleep',lambda t:clock.__setitem__(0,clock[0]+t)):
                self.request()
                if recover:
                    cli.finish_controls(self.control,links,timeout=2.)
                    self.assertEqual(self.receipt(),'completed');self.assertGreaterEqual(links[0].reads,3)
                    self.assertTrue(all(clock[0]-s['latest']['monotonic']<.5 for s in observations.values()))
                else:
                    with self.assertRaisesRegex(RuntimeError,'시간 초과'):cli.finish_controls(self.control,links,timeout=1.)
                    self.assertEqual(self.receipt(),'failed')

    def test_stop_predicate_preserves_half_second_boundary(self):
        link=FakeLink()
        with patch.object(cli.time,'monotonic',return_value=100.):value=link.state()
        self.assertTrue(cli.stopped(value,now=100.499))
        self.assertFalse(cli.stopped(value,now=100.5))
        self.assertFalse(cli.stopped(value,now=99.9))


class SimpleLogTests(unittest.TestCase):
    def test_default_short_time_and_details_keep_full_precision(self):
        event={'at':1792639025.379,'arm':'arm2','status':'A_DONE','elapsed_s':14.123,
               'timing_summary':{'total_seconds':14.123,'completed':True,'phases':[]}}
        with redirect_stdout(io.StringIO()) as out:monitor.print_event(event)
        text=out.getvalue();self.assertRegex(text,r'^\[\d\d:\d\d:\d\d\]');self.assertIn('소요 14.1초',text)
        self.assertNotIn('+14.',text);self.assertNotIn('2026-',text)
        with redirect_stdout(io.StringIO()) as out:monitor.print_event(event,details=True)
        self.assertIn('2026-',out.getvalue());self.assertIn('+14.123s',out.getvalue())
