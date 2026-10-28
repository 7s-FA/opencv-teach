"""Device-free concurrency, persistence and whole-job timing regressions."""
from contextlib import redirect_stdout
import io
import os
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from tests.test_episode_cli import cli
from tests.test_chain_parallel_start import ChainParallelStartTests as ChainFixture
from tests.test_work_monitor import monitor
from episode_timing import JobTiming


class LatencyTests(unittest.TestCase):
    def test_independent_links_open_together_and_all_attempts_drain_on_failure(self):
        barrier=threading.Barrier(2);finished=[]
        class Link:
            def __init__(self,key):self.key=key
            def open(self):
                barrier.wait(timeout=2);finished.append(self.key)
                if self.key==0:raise RuntimeError('connection rejected')
        with self.assertRaisesRegex(RuntimeError,'connection rejected'):cli.open_links(Link(0),Link(1))
        self.assertCountEqual(finished,[0,1])

    def chain(self):
        fixture=ChainFixture();fixture.setUp();self.addCleanup(fixture.doCleanups)
        return fixture

    def test_configuration_of_both_arms_overlaps_without_worker_consuming_controls(self):
        fixture=self.chain();barrier=threading.Barrier(2);threads=[]
        for link in fixture.links.values():
            original=link.rpc
            def call(method,args=None,original=original):
                if method=='configure':barrier.wait(timeout=2)
                if method=='command':threads.append(threading.current_thread())
                return original(method,args)
            link.rpc=call
        fixture.build()
        self.assertTrue(threads)
        self.assertTrue(all(t is threading.current_thread() for t in threads))

    def test_chain_linear_starts_before_either_safe_arrival(self):
        fixture=self.chain();seen=[]
        for link in fixture.links.values():
            link.auto_finish=False;original=link.state
            def state(link=link,original=original):
                linear_started=any(op=='ensure' for op,args in fixture.linear.calls)
                if link.pending and linear_started:
                    seen.append(link.arm);link.mode='HOLD';link.pending=False;link.completed=link.request;link.index=99
                return original()
            link.state=state
        # If the stage waits for arm arrival this cannot finish.
        fixture.build()
        self.assertIn('arm2',seen);self.assertIn('arm3',seen)
        self.assertEqual(sum(op=='ensure' for op,args in fixture.linear.calls),1)

    def test_peer_prepare_failure_never_moves_either_arm_or_linear(self):
        fixture=self.chain();original=fixture.links['arm3'].rpc
        def call(method,args=None):
            if method=='configure':raise RuntimeError('bad peer settings')
            return original(method,args)
        fixture.links['arm3'].rpc=call
        with self.assertRaisesRegex(RuntimeError,'bad peer settings'):fixture.build()
        self.assertFalse(any(action in ('arm','move','play') for arm,action in fixture.trace))
        self.assertFalse(any(op=='ensure' for op,args in fixture.linear.calls))


class TimingTests(unittest.TestCase):
    def test_chain_summary_separates_all_phases_and_uses_monotonic_time(self):
        with patch('episode_timing.time.monotonic',side_effect=[100.,100.,110.,140.,142.,150.,175.]):
            timer=JobTiming();timer.begin('build_load_a')
            timer.record('arm2','CHAIN_STARTED:build_load_a')
            timer.record('arm2','EPISODE_STARTED')
            timer.record('arm2','BUILD_DONE')
            timer.record('arm3','CHAIN_LOAD_STARTED:build_load_a')
            timer.record('arm3','EPISODE_STARTED')
            summary=timer.record('arm3','CHAIN_DONE')['timing_summary']
        self.assertEqual([(p['phase'],p['seconds']) for p in summary['phases']],
                         [('build_prepare',10.),('build',30.),('load_prepare',10.),('load',25.)])
        self.assertEqual(summary['total_seconds'],75.);self.assertTrue(summary['completed'])

    def test_failure_records_partial_phase_without_reporting_unstarted_load(self):
        with patch('episode_timing.time.monotonic',side_effect=[10.,12.,20.]):
            timer=JobTiming();timer.begin('build_load_b');timer.record('arm2','EPISODE_STARTED')
            summary=timer.record('arm2','FAILED:inspection')['timing_summary']
        self.assertEqual([p['phase'] for p in summary['phases']],['build_prepare','build'])
        self.assertFalse(summary['completed']);self.assertFalse(summary['phases'][-1]['completed'])

    def test_reset_does_not_label_interrupted_assembly_as_completed(self):
        with patch('episode_timing.time.monotonic',side_effect=[10.,12.,20.,25.]):
            timer=JobTiming();timer.begin('build_load_a');timer.record('arm2','EPISODE_STARTED')
            timer.record('arm2','ARM_RESET:arm2:RESET_STARTED')
            summary=timer.record('arm2','RESET_DONE')['timing_summary']
        self.assertEqual(summary['phases'][-1]['phase'],'recovery')
        self.assertFalse(summary['phases'][-2]['completed'])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        self.patch=patch.object(cli,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)

    def status(self):
        status=cli.Status('arm2','A',False);self.addCleanup(status.close);return status

    def test_progress_does_not_wait_for_disk_but_terminal_waits_and_order_is_preserved(self):
        status=self.status();started=threading.Event();release=threading.Event();done=threading.Event()
        original=status._write
        def write(*args):started.set();release.wait(3);return original(*args)
        errors=[]
        with patch.object(status,'_write',side_effect=write):
            status.emit('PLAN_STARTED');self.assertTrue(started.wait(1))
            status.emit('EPISODE_STARTED')
            def finish():
                try:status.emit('DONE');status.flush_final()
                except Exception as exc:errors.append(exc)
                finally:done.set()
            thread=threading.Thread(target=finish);thread.start()
            try:self.assertFalse(done.wait(.05))
            finally:release.set();thread.join(3)
        self.assertFalse(errors);self.assertTrue(done.is_set())
        log=json.loads(status.log.read_text())
        self.assertEqual([e['status'] for e in log['events']],['A_PLAN_STARTED','A_EPISODE_STARTED','A_DONE'])
        self.assertIn('timing_summary',log)
        self.assertTrue(all('at_iso' in e and 'elapsed_s' in e for e in log['events']))

    def test_failed_progress_write_prevents_success_and_flush_propagates_failure(self):
        status=cli.Status('arm2','A',False)
        with patch.object(status,'_write',side_effect=OSError('disk full')),redirect_stdout(io.StringIO()) as output:
            status.emit('PLAN_STARTED');status.emit('DONE')
            with self.assertRaisesRegex(OSError,'disk full'):status.flush_final()
            with self.assertRaisesRegex(OSError,'disk full'):status.close()
        self.assertNotIn('A_DONE',output.getvalue())

    def test_handoff_reuses_publisher_and_job_clock_but_keeps_arm_run_ids(self):
        from status_transport import OrderedStatusWriter,StatusOutput
        status=self.status();process=Mock();reader,writer=os.pipe();self.addCleanup(os.close,reader)
        os.set_blocking(writer,False);process.stdin=os.fdopen(writer,'w');status.process=process
        status.ros_output=StatusOutput(process=process);status.ros_writer=OrderedStatusWriter(status.ros_output.send)
        status.timing.begin('build_load_a');status.emit('BUILD_DONE')
        load=status.handoff('arm3','A');self.addCleanup(load.close)
        load.emit('CHAIN_LOAD_STARTED:build_load_a');load.emit('CHAIN_DONE');load.flush_final()
        load.ros_writer.flush()
        self.assertIs(load.process,process);process.wait.assert_not_called()
        self.assertIs(load.timing,status.timing);self.assertNotEqual(load.run_id,status.run_id)
        rows=[json.loads(line) for line in os.read(reader,65536).splitlines()]
        self.assertEqual([r['arm'] for r in rows],['arm2','arm3','arm3'])
        self.assertEqual(json.loads(load.log.read_text())['job_id'],json.loads(status.log.read_text())['job_id'])

    def test_monitor_prints_millisecond_timestamp_elapsed_and_completion_breakdown(self):
        status=self.status();status.timing.begin('build_load_a');status.emit('EPISODE_STARTED');status.emit('DONE');status.flush_final()
        event=json.loads(status.log.read_text())['events'][-1]
        with redirect_stdout(io.StringIO()) as output:monitor.print_event(event,details=True)
        text=output.getvalue();self.assertIn('작업 소요시간',text);self.assertIn('조립 전 준비',text)
        self.assertIn('전체',text);self.assertIn(event['at_iso'],text);self.assertIn('s]',text)
