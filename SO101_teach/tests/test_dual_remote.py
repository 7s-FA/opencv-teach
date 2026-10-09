import json,tempfile,threading,time,unittest
from pathlib import Path
from copy import deepcopy
from unittest.mock import Mock,patch
from tests import test_remote as fixtures
from so101_teach.remote_server import Runtime,Server,Handler,runtime_directory
from so101_teach.remote_client import RemoteLink,RemoteMotionSession
from so101_teach.remote_config import configuration_bundle
from so101_teach.configuration import JigCatalog
from so101_teach.vision import PoseLatch
from tests.fixtures import DATA,load_profile
from types import SimpleNamespace

class DualRemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.runtimes=[];self.servers=[];self.links=[]
        p,cal,ref=load_profile();self.bundle=configuration_bundle(SimpleNamespace(profile=p,data_dir=DATA,catalog=JigCatalog(DATA),reference=ref,pose_latch=PoseLatch(),active_jig='pallet'))
        for arm in ('arm2','arm3'):
            r=Runtime(Path(self.temp.name)/arm,motion_factory=fixtures.FakeMotion,camera_factory=fixtures.FakeCamera,robot_id=arm)
            bundle=deepcopy(self.bundle);bundle['profile']['robot_id']=arm;bundle['profile']['port']='fake-'+arm;bundle['profile']['mode']='follower';r.configure(bundle)
            lease=r.acquire()['lease'];server=Server(('127.0.0.1',0),Handler);server.runtime=r;server.token='test-'+arm
            threading.Thread(target=server.serve_forever,daemon=True).start()
            link=RemoteLink({'host':'localhost','user':'robot'},robot_id=arm);link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token;link.lease=lease
            self.runtimes.append(r);self.servers.append(server);self.links.append(link)
    def tearDown(self):
        for s in self.servers:s.shutdown();s.server_close()
        for r in self.runtimes:r.close()
        self.temp.cleanup()
    def test_two_live_sessions_have_separate_commands_and_disconnects(self):
        sessions=[RemoteMotionSession(link,r.profile['port'],r.cal) for link,r in zip(self.links,self.runtimes)]
        for session in sessions:session.start()
        a,b=self.runtimes
        a.session.request=Mock(return_value=11);b.session.request=Mock(return_value=22)
        a.session.state='MOVING';a.session.program_active.set();a.session.index=4
        sessions[1].request('move',[b.ref.middle]);b.session.request.assert_called_once_with('move',[b.ref.middle]);a.session.request.assert_not_called()
        self.assertTrue(a.session.program_active.is_set());self.assertEqual(a.session.index,4)
        sessions[1].close();self.assertFalse(b.session.running);self.assertTrue(a.session.running);self.assertEqual(a.session.state,'MOVING')
    def test_wrong_arm_bundle_is_rejected_before_touching_running_arm(self):
        a,b=self.runtimes;old=b.profile.copy()
        with self.assertRaisesRegex(ValueError,'다른 로봇팔'):b.configure(self.bundle)
        self.assertEqual(b.profile,old)
    def test_heartbeats_and_faults_are_scoped_to_each_runtime(self):
        for link,r in zip(self.links,self.runtimes):RemoteMotionSession(link,r.profile['port'],r.cal).start()
        a,b=self.runtimes;a.session.state='MOVING';a.session.program_active.set();a.last_heartbeat=time.monotonic()-3
        deadline=time.monotonic()+1
        while not a.detached and time.monotonic()<deadline:b.heartbeat(b.lease,True);time.sleep(.02)
        self.assertTrue(a.detached);self.assertTrue(a.session.hold_requested.is_set());self.assertFalse(b.detached);self.assertFalse(b.session.hold_requested.is_set())
    def test_same_request_id_is_independent_between_arms(self):
        for link,r in zip(self.links,self.runtimes):RemoteMotionSession(link,r.profile['port'],r.cal).start();r.session.request=Mock(return_value=1)
        for r in self.runtimes:
            request={'lease':r.lease,'id':'same-request','method':'command','args':{'action':'arm'}}
            self.assertTrue(r.rpc(request)['ok']);self.assertTrue(r.rpc(request)['ok']);r.session.request.assert_called_once_with('arm',None)
    def test_runtime_directories_do_not_share_credentials_or_configuration(self):
        self.assertNotEqual(runtime_directory('arm2'),runtime_directory('arm3'))
        with self.assertRaises(ValueError):runtime_directory('../arm2')
