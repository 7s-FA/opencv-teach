"""Fault injection at durable completion, cache projection and network boundaries."""
import errno,io,json,os,stat,tempfile,threading,time,unittest,uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from tests.test_episode_cli import cli
from tests.test_supervision_storage import StorageBoundaryTests
from tests.test_action_contract import Goal,Process
from supervision_session import RequestLedger
import supervision_session
from status_transport import OrderedStatusWriter
from so101_teach.pc_compute import ComputeClient
import so101_teach.pc_compute as compute

class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'data').mkdir()
        p=patch.object(cli,'ROOT',self.root);p.start();self.addCleanup(p.stop)
        self.status=cli.Status('arm2','A',False)
    def tearDown(self):
        try:self.status.close()
        except OSError:pass
    def test_primary_failure_before_or_after_replace_never_advertises_done(self):
        self.status.publish('ACCEPTED');real=cli.atomic_json
        for after in (False,True):
            # A failed writer deliberately poisons this run; each boundary gets a fresh run.
            if after:
                self.tearDown();self.status=cli.Status('arm2','A',False);self.status.publish('ACCEPTED')
            def fail(path,value):
                if path==self.status.log:
                    if after:
                        sync=os.fsync
                        def fail_directory(fd):
                            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EIO,'primary directory fsync')
                            sync(fd)
                        with patch('os.fsync',side_effect=fail_directory):return real(path,value)
                    raise OSError(errno.EIO,'primary commit not confirmed')
                real(path,value)
            with patch.object(cli,'atomic_json',side_effect=fail),redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(OSError):self.status.publish('DONE')
            self.assertNotIn('A_DONE',out.getvalue());self.assertFalse(self.status.final_committed)
            self.assertEqual(json.loads(self.status.path.read_text())['status'],'A_ACCEPTED')
    def test_latest_fsync_after_replace_does_not_undo_primary_completion(self):
        self.status.publish('ACCEPTED');real=cli.atomic_json;sync=os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EIO,'directory fsync after replace')
            sync(fd)
        def write(path,value):
            if path==self.status.path:
                with patch('os.fsync',side_effect=fail_directory):return real(path,value)
            return real(path,value)
        with patch.object(cli,'atomic_json',side_effect=write),redirect_stdout(io.StringIO()) as out:self.status.publish('DONE')
        self.assertTrue(self.status.final_committed);self.assertIn('A_DONE',out.getvalue())
        self.assertEqual(json.loads(self.status.log.read_text())['events'][-1]['status'],'A_DONE')
    def test_failed_first_projection_recovers_but_cannot_replace_newer_owner(self):
        real=cli.atomic_json
        def fail(path,value):
            if path==self.status.path:raise OSError(errno.ENOSPC,'cache full')
            real(path,value)
        with patch.object(cli,'atomic_json',side_effect=fail):self.status.publish('ACCEPTED')
        self.status.publish('BUILD_DONE');self.assertEqual(json.loads(self.status.path.read_text())['run_id'],self.status.run_id)
        other=cli.Status('arm2','A',False)
        try:
            other.publish('ACCEPTED');self.status.publish('DONE')
            self.assertEqual(json.loads(self.status.path.read_text())['run_id'],other.run_id)
        finally:other.close()
    def test_final_sender_failure_is_warning_but_precompletion_failure_raises(self):
        def fail(value):raise OSError('disconnected')
        self.status.ros_writer=OrderedStatusWriter(fail)
        self.status.publish('ACCEPTED')
        with self.assertRaises(OSError):self.status.ros_writer.flush()
        with self.assertRaises(OSError):self.status.publish('BUILD_DONE')
        self.status.publish('DONE');self.status.close()
        self.assertTrue(self.status.final_committed)
    def test_corrupt_derived_latest_does_not_undo_durable_completion(self):
        self.status.path.write_text('[]')
        self.status.publish('ACCEPTED');self.status.publish('DONE')
        self.assertTrue(self.status.final_committed)
        self.assertEqual(json.loads(self.status.log.read_text())['events'][-1]['status'],'A_DONE')
    def test_stuck_optional_publisher_is_reaped_after_completion(self):
        from unittest.mock import Mock
        import subprocess
        self.status.publish('DONE');process=Mock()
        process.wait.side_effect=[subprocess.TimeoutExpired('publisher',4),subprocess.TimeoutExpired('publisher',2),0]
        self.status.process=process;self.status.close()
        process.terminate.assert_called_once();process.kill.assert_called_once()
        self.assertIsNone(self.status.process)
    def test_journal_export_failure_after_final_commit_preserves_done(self):
        self.status.publish('ACCEPTED')
        for n in range(33):self.status.publish('EPISODE_STARTED')
        self.status.flush_records();journal=self.status.run_journal
        with patch.object(journal,'materialize',side_effect=OSError(errno.ENOSPC,'export full')),redirect_stdout(io.StringIO()) as out:
            self.status.publish('DONE');self.status.close()
        from so101_teach.execution_records import read_run
        self.assertIn('A_DONE',out.getvalue());self.assertEqual(read_run(self.status.log)['events'][-1]['status'],'A_DONE')

class ResultReadAndReplaceTests(unittest.TestCase):
    setUpClass=classmethod(lambda cls:StorageBoundaryTests.setUpClass())
    setUp=StorageBoundaryTests.setUp
    replay=StorageBoundaryTests.replay
    def test_final_replace_fsync_failure_and_query_read_error_repair_without_spawn(self):
        goal=Goal('BUILD_A');self.s.ledger.accept(goal.meta,'arm2');sync=os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError(errno.EIO,'completion directory fsync')
            sync(fd)
        real_read=Path.read_text
        def fail_read(path,*a,**kw):
            if path==self.s.ledger.path(goal.meta):raise OSError(errno.EIO,'result query read')
            return real_read(path,*a,**kw)
        with patch.object(self.f.module.subprocess,'Popen') as start:
            with patch.object(supervision_session.os,'fsync',side_effect=fail_directory):
                with self.assertRaises(OSError):self.s.ledger.finish(goal.meta,'arm2',True,'IDLE')
                self.assertEqual(self.s.ledger.lookup(goal.meta,'arm2')['state'],'completed')
                for _ in range(3):self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
            with patch.object(Path,'read_text',fail_read):
                for _ in range(3):self.assertEqual(self.replay(goal).message,'STORAGE_PENDING')
            self.assertEqual(self.replay(goal).message,'IDLE');self.assertFalse(self.s.ledger.pending_results)
            for _ in range(3):
                duplicate=Goal('BUILD_A');duplicate.meta={**goal.meta,'attempt_id':uuid.uuid4().hex};duplicate.request.command=json.dumps(duplicate.meta)
                self.assertTrue(self.s.goal_callback(duplicate.request));self.assertEqual(self.s.execute_callback(duplicate).message,'IDLE')
            start.assert_not_called()
        self.assertEqual(RequestLedger(self.f.temp.name,'restarted').response(goal.meta,'arm2'),(True,'IDLE'))

class NetworkDeadlineTests(unittest.TestCase):
    def exercise(self,protocol='HTTP/1.1',headers=False,cancel=False):
        started=threading.Event();stop=threading.Event();requests=[]
        class Slow(BaseHTTPRequestHandler):
            protocol_version=protocol
            def log_message(self,*a):pass
            def do_POST(self):
                req=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(req);started.set()
                reply={k:req[k] for k in ('request_id','job_id','contract','frame_id','criteria_id')};reply['value']='late'
                raw=json.dumps(reply).encode()
                try:
                    if headers:
                        for part in (b'HTTP/1.1 200 OK\r\n',b'X-Slow: a\r\n',b'X-Slow: b\r\n',b'X-Slow: c\r\n',b'\r\n'):
                            self.wfile.write(part);self.wfile.flush();time.sleep(.06)
                    else:
                        self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
                        for part in range(0,len(raw),50):
                            self.wfile.write(raw[part:part+50]);self.wfile.flush();time.sleep(.06)
                except OSError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Slow);server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            with patch.object(compute,'contract',return_value='c'*64):
                c=ComputeClient(dict(host='127.0.0.1',port=server.server_port,job_id='a'*32,token='b'*64,contract='c'*64),stop=stop)
            timer=None
            if cancel:
                timer=threading.Timer(.08,stop.set);timer.start()
            began=time.monotonic()
            with self.assertRaises((TimeoutError,RuntimeError)):c.call('echo',timeout=2 if cancel else .12)
            self.assertLess(time.monotonic()-began,.25);self.assertTrue(c.failed);self.assertEqual(c.metrics,[])
            with self.assertRaises(RuntimeError):c.call('echo')
            self.assertEqual(len(requests),1);self.assertFalse(any(t.name=='pc-compute-deadline' for t in threading.enumerate()))
            if timer:timer.join()
        finally:server.shutdown();server.server_close();thread.join()
    def test_body_trickle_http_10(self):self.exercise('HTTP/1.0')
    def test_body_trickle_http_11(self):self.exercise()
    def test_header_trickle(self):self.exercise(headers=True)
    def test_cancel_while_reading_body(self):self.exercise(cancel=True)
    def test_cancel_before_connection_assigns_socket_still_interrupts_read(self):
        import socket
        stop=threading.Event();left,right=socket.socketpair();left.settimeout(.5)
        class DelayedConnection:
            sock=None
            def request(self,*a):
                stop.set();time.sleep(.08);self.sock=left
            def getresponse(self):
                left.recv(1)
                return type('Reply',(),{'status':200,'read':lambda self,n:b'{}','close':lambda self:None})()
        with patch.object(compute,'contract',return_value='c'*64):
            c=ComputeClient(dict(host='127.0.0.1',port=1,job_id='a'*32,token='b'*64,contract='c'*64),stop=stop)
        began=time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError,'취소'):c._exchange(DelayedConnection(),b'',{},began+2)
            self.assertLess(time.monotonic()-began,.25)
        finally:left.close();right.close()
    def test_cancel_waiting_for_other_call(self):
        with patch.object(compute,'contract',return_value='c'*64):
            stop=threading.Event();c=ComputeClient(dict(host='127.0.0.1',port=1,job_id='a'*32,token='b'*64,contract='c'*64),stop=stop)
        c.call_lock.acquire();timer=threading.Timer(.03,stop.set);timer.start();began=time.monotonic()
        try:
            with self.assertRaisesRegex(RuntimeError,'취소'):c.call('echo',timeout=2)
            self.assertLess(time.monotonic()-began,.2)
        finally:c.call_lock.release();timer.join()

class WarningProtocolTests(unittest.TestCase):
    setUpClass=classmethod(lambda cls:StorageBoundaryTests.setUpClass())
    setUp=StorageBoundaryTests.setUp
    def test_only_explicit_warning_preserves_terminal_and_exit_status_still_gates(self):
        for tail,code,success in [('EXECUTOR_WARNING:late ACK',0,True),('EXECUTOR_WARNING:late ACK',1,False),('A_FAILED:disk',0,False),('unknown trailing output',0,False)]:
            with self.subTest(tail=tail,code=code):
                goal=Goal('BUILD_A');self.assertTrue(self.s.goal_callback(goal.request))
                with patch.object(self.f.module.subprocess,'Popen',return_value=Process(['READY_TO_COMPLETE','A_DONE',tail],code)):
                    self.assertEqual(self.s.execute_callback(goal).success,success)
