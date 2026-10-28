import json,socket,threading,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_episode_cli import cli
from status_transport import OrderedStatusWriter,StatusSocketClient,StatusSocketRelay

class StatusTransportTests(unittest.TestCase):
    def test_arm_handoff_does_not_wait_behind_slow_status_delivery(self):
        entered=threading.Event();release=threading.Event();values=[]
        def send(value):entered.set();release.wait(2);values.append(value)
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)):
            (Path(folder)/'data').mkdir();first=cli.Status('arm2','B',False);second=None
            first.ros_writer=OrderedStatusWriter(send)
            try:
                first.publish('BUILD_DONE');self.assertTrue(entered.wait(1));started=time.monotonic()
                second=first.handoff('arm3','B');self.assertLess(time.monotonic()-started,.3)
                second.publish('CHAIN_LOAD_STARTED');release.set();second.ros_writer.flush()
                self.assertEqual([v['arm'] for v in values],['arm2','arm3'])
            finally:
                release.set();first.close()
                if second:second.close()
    def test_parent_relay_preserves_order_and_arm_handoff_without_child_ros(self):
        published=[];relay=StatusSocketRelay(lambda arm,text:published.append((arm,text)))
        self.addCleanup(relay.close)
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)),patch.object(cli.subprocess,'Popen',side_effect=AssertionError('ROS must be reused')):
            (Path(folder)/'data').mkdir()
            status=cli.Status('arm2','B',True,status_fd=relay.child.fileno())
            try:
                status.publish('ACCEPTED');status.publish('EPISODE_STARTED')
                second=status.handoff('arm3','B')
                try:second.publish('BUILD_DONE');second.publish('DONE')
                finally:second.close()
            finally:status.close()
        self.assertEqual(published,[('arm2','B_ACCEPTED'),('arm2','B_EPISODE_STARTED'),('arm3','B_BUILD_DONE'),('arm3','B_DONE')])
    def test_slow_transport_does_not_block_enqueue_and_queue_is_bounded(self):
        entered=threading.Event();release=threading.Event();values=[]
        def send(value):entered.set();release.wait(2);values.append(value)
        writer=OrderedStatusWriter(send,capacity=2)
        try:
            writer.submit({'n':1});self.assertTrue(entered.wait(1));start=time.monotonic()
            writer.submit({'n':2});writer.submit({'n':3});self.assertLess(time.monotonic()-start,.2)
            with self.assertRaisesRegex(RuntimeError,'포화'):writer.submit({'n':4})
            release.set();writer.flush();self.assertEqual(values,[{'n':1},{'n':2},{'n':3}])
        finally:release.set();writer.close()
    def test_send_failure_cannot_be_reported_as_flushed(self):
        def send(value):raise OSError('transport failure')
        writer=OrderedStatusWriter(send)
        try:
            writer.submit({'n':1})
            with self.assertRaisesRegex(OSError,'transport failure'):writer.flush()
            with self.assertRaisesRegex(OSError,'transport failure'):writer.submit({'n':2})
        finally:writer.close()
    def test_wrong_sequence_or_arm_never_publishes(self):
        for value in ({'arm':'arm2','status':'B_DONE','sequence':2},{'arm':'other','status':'B_DONE','sequence':1}):
            calls=[];relay=StatusSocketRelay(lambda *args:calls.append(args));relay.child.settimeout(1)
            try:
                relay.child.sendall((json.dumps(value)+'\n').encode());self.assertEqual(relay.child.recv(4096),b'')
                self.assertFalse(calls)
            finally:relay.close()
    def test_pipe_timeout_is_finite_without_waiting_in_control_thread(self):
        import os
        from status_transport import write_pipe
        read,write=os.pipe();os.set_blocking(write,False)
        try:
            while True:
                try:os.write(write,b'x'*4096)
                except BlockingIOError:break
            start=time.monotonic()
            with self.assertRaises(TimeoutError):write_pipe(write,{'status':'x'},timeout=.02)
            self.assertLess(time.monotonic()-start,.2)
        finally:os.close(read);os.close(write)
