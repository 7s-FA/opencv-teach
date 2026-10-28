"""Real IPC/HTTP read paths under a simulated executing owner; no hardware."""
from collections import OrderedDict
from copy import deepcopy
import hashlib,json,queue,socket,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from desktop_monitor.source import Source
from desktop_monitor.client import WorkObserver
from so101_teach.remote_server import Runtime,Server,Handler
from so101_teach.runtime_version import manifest

class ObserverSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);(self.root/'data/episode-cli-runs').mkdir(parents=True)
    def write(self,name,value):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value));return path
    def test_log_events_keep_real_sequence_and_do_not_write_files(self):
        key='a'*32;self.write('data/episode-cli-runs/'+key+'.json',{'run_id':key,'events':[{'at':10.,'arm':'arm2','status':'B_ACCEPTED'},{'at':11.,'arm':'arm2','status':'B_JIG_POSE:j:x_mm=1:y_mm=2:yaw_deg=3'}]})
        source=Source(self.root)
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        first=source.snapshot();second=source.snapshot()
        self.assertEqual(first['events'],second['events']);self.assertEqual(first['events'][-1]['sequence'],2)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_partial_log_does_not_hide_other_records(self):
        self.write('data/episode-cli-runs/'+('a'*32)+'.json',{'run_id':'a'*32,'events':[{'at':1.,'arm':'arm2','status':'B_ACCEPTED'}]})
        path=self.write('data/episode-cli-runs/'+('b'*32)+'.json',{});path.write_text('{')
        result=Source(self.root).snapshot();self.assertEqual(len(result['events']),1);self.assertTrue(result['errors'])
    def test_source_process_rejects_control_operations(self):
        script=Path(__file__).parents[1]/'desktop_monitor/source.py'
        result=subprocess.run([sys.executable,'-B',str(script),str(self.root)],input='{"operation":"command","action":"play"}\n',text=True,capture_output=True,timeout=5)
        self.assertFalse(json.loads(result.stdout)['ok']);self.assertEqual(list((self.root/'data/episode-cli-runs').iterdir()),[])
    def test_monitor_package_does_not_change_pi_runtime_identity(self):
        root=Path(__file__).parents[1];before=manifest(root)
        self.assertFalse(any('desktop_monitor' in p for p in before['files']))
        self.assertEqual(before,manifest(root))

    def test_monitor_open_poll_close_cannot_change_an_executing_owner(self):
        from tests.test_remote import FakeCamera,FakeMotion
        data=self.root/'data';runtime=Runtime(data,motion_factory=FakeMotion,camera_factory=FakeCamera)
        self.addCleanup(runtime.close)
        running=threading.Event();running.set();hold=threading.Event();owner_stop=threading.Event()
        session=SimpleNamespace(running=True,state='MOVING',events=queue.Queue(),index=0,heartbeat=0,hold_requested=hold,
            command_pending=threading.Event(),program_active=running,close=lambda:None,join=lambda timeout:True)
        runtime.session=session;runtime.last_heartbeat=time.monotonic();runtime.lease='existing-executor';runtime.detached=False;runtime.profile={'sentinel':'unchanged'}
        camera=FakeCamera();camera.start();camera.processing_enabled=True
        frame=np.full((120,160,3),120,np.uint8);camera.observation=(frame,{},time.monotonic());camera.preview_frame=(frame,camera.observation[2]);runtime.camera=camera
        def owner():
            while not owner_stop.wait(.02):
                runtime.heartbeat('existing-executor',True);session.index+=1
                camera.observation=(frame,{},time.monotonic());camera.preview_frame=(frame,camera.observation[2])
        thread=threading.Thread(target=owner,daemon=True);thread.start();self.addCleanup(lambda:(owner_stop.set(),thread.join(1)))
        requests=[]
        class WatchHandler(Handler):
            def do_POST(handler):
                requests.append(handler.path)
                self.assertIn(handler.path,('/camera','/preview'))
                return super(WatchHandler,handler).do_POST()
        server=Server(('127.0.0.1',0),WatchHandler);server.runtime=runtime;server.token='fixture-token'
        http_thread=threading.Thread(target=server.serve_forever,daemon=True);http_thread.start();self.addCleanup(lambda:(server.shutdown(),server.server_close()))
        self.write('data/remote-server.json',{'port':server.server_port,'instance':'fixture'})
        (data/'remote-token').write_text('fixture-token')
        unix=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);unix.bind(str(data/'episode-cli.sock'));unix.listen();unix.settimeout(.1);self.addCleanup(unix.close)
        controls=[];end=threading.Event()
        def serve():
            while not end.is_set():
                try:c,_=unix.accept()
                except socket.timeout:continue
                except OSError:return
                with c:
                    value=c.recv(4096);controls.append(value)
                    c.sendall((json.dumps({'info':{'run_id':'a'*32,'arm':'arm2'},'paused':False,'job_id':'job'})+'\n').encode())
        control_thread=threading.Thread(target=serve,daemon=True);control_thread.start();self.addCleanup(lambda:(end.set(),control_thread.join(1)))
        self.write('data/episode-cli-runs/'+('a'*32)+'.json',{'run_id':'a'*32,'job_id':'job','record_kind':'execution','events':[{'at':time.time(),'arm':'arm2','status':'B_ACCEPTED'}]})
        protected={p:p.read_bytes() for p in data.rglob('*') if p.is_file()}
        observer=WorkObserver({'host':'example.invalid','user':'test','app_dir':str(self.root),'python':sys.executable})
        script=Path(__file__).parents[1]/'desktop_monitor/source.py'
        observer.process=subprocess.Popen([sys.executable,'-u','-B',str(script),str(self.root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            with patch.object(runtime,'rpc',side_effect=AssertionError('No RPC')),patch.object(runtime,'acquire',side_effect=AssertionError('No acquire')):
                initial=session.index
                for _ in range(3):
                    value=observer.poll_once();self.assertTrue(value['connected']);self.assertIsNotNone(value['frame']);time.sleep(.03)
                observer.close();observer.disconnect();time.sleep(.06)
                self.assertGreater(session.index,initial);self.assertFalse(hold.is_set());self.assertTrue(camera.running)
                self.assertEqual(runtime.lease,'existing-executor');self.assertFalse(runtime.detached);self.assertTrue(running.is_set())
                self.assertEqual(runtime.profile,{'sentinel':'unchanged'})
        finally:observer.close();observer.disconnect()
        self.assertTrue(requests);self.assertEqual(set(requests),{'/camera','/preview'});self.assertEqual(set(controls),{b'inspect'})
        self.assertEqual(protected,{p:p.read_bytes() for p in data.rglob('*') if p.is_file()})
    def test_packets_send_only_new_events_and_unchanged_metadata_is_omitted(self):
        key='a'*32;path=self.write('data/episode-cli-runs/'+key+'.json',{'run_id':key,'events':[{'at':1.,'arm':'arm2','status':'B_ACCEPTED'}]})
        source=Source(self.root);first=source.packet();second=source.packet()
        self.assertTrue(first['events_reset']);self.assertEqual(len(first['events']),1)
        self.assertFalse(second['events_reset']);self.assertEqual(second['events'],[]);self.assertNotIn('runs',second)
        doc=json.loads(path.read_text());doc['events'].append({'at':2.,'arm':'arm2','status':'B_STEP_DONE:1/28:시작'})
        path.write_text(json.dumps(doc))
        with patch('desktop_monitor.source.time.monotonic',return_value=source.state_checked+.51):third=source.packet()
        self.assertEqual(len(third['events']),1);self.assertEqual(third['events'][0]['sequence'],2)

    def test_source_transports_timing_summary_for_terminal_equivalent_log(self):
        from desktop_monitor.model import record_lines
        key='a'*32;timing={'total_seconds':110.,'completed':False,'phases':[{'phase':'load','seconds':24.4,'completed':False}]}
        self.write('data/episode-cli-runs/'+key+'.json',{'run_id':key,'events':[{'at':10.,'arm':'arm3','status':'A_FAILED:관측 부품 없음','timing_summary':timing}]})
        events,runs,errors=Source(self.root).records({'info':None})
        self.assertEqual(events[0]['timing_summary'],timing)
        text=''.join(r[0] for r in record_lines({'events':events,'runs':runs},None))
        self.assertIn('구간: 하차 24.4초 (중단)',text);self.assertIn('소요 110.0초',text)
