import tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from so101_teach.remote_server import Runtime,Server,Handler
from so101_teach.remote_client import RemoteLink
from tests import test_arm_workspaces as workspace_fixtures
from tests import test_remote as remote_fixtures

class ConnectAllHTTPTests(unittest.TestCase):
    setUp=workspace_fixtures.ArmWorkspacesTests.setUp
    tearDown=workspace_fixtures.ArmWorkspacesTests.tearDown
    def test_one_click_prepares_two_independent_read_connections_without_motion(self):
        runtimes={};servers=[];methods=[]
        def opened(link):
            arm=link.robot_id;r=Runtime(self.data/('runtime-'+arm),motion_factory=remote_fixtures.FakeMotion,camera_factory=remote_fixtures.FakeCamera,robot_id=arm);runtimes[arm]=r
            server=Server(('127.0.0.1',0),Handler);server.runtime=r;server.token='test-'+arm;servers.append(server);threading.Thread(target=server.serve_forever,daemon=True).start()
            original=r.call
            def call(method,args):methods.append((arm,method));return original(method,args)
            r.call=call
            link.base=f'http://127.0.0.1:{server.server_port}';link.token=server.token;link.instance=r.instance
            lease=link.http('/acquire',{});link.lease=lease['lease'];link.event_id=lease['event_id'];link.health={'devices':{'serial':[],'cameras':[]}}
            link.state_thread=threading.Thread(target=link.poll_state,daemon=True);link.state_thread.start();return link
        try:
            for app in self.manager.apps.values():app.remote_mode=True
            with patch.object(RemoteLink,'open',opened):
                self.manager.connect_all();deadline=time.monotonic()+6
                while not all(a.session and a.session.running for a in self.manager.apps.values()) and time.monotonic()<deadline:self.root.update();time.sleep(.02)
            self.assertEqual(set(runtimes),{'arm2','arm3'})
            self.assertTrue(all(a.session and a.session.running for a in self.manager.apps.values()),[a.message.get() for a in self.manager.apps.values()])
            self.assertTrue(all(r.session.state=='READ_ONLY' for r in runtimes.values()))
            self.assertFalse(any(method=='command' for _,method in methods));before=list(methods);self.manager.connect_all();self.assertEqual(methods,before)
            for a in self.manager.apps.values():a.session.close()
            until=time.monotonic()+2
            while any(a.session.running for a in self.manager.apps.values()) and time.monotonic()<until:self.root.update();time.sleep(.02)
        finally:
            for app in self.manager.apps.values():
                if app.remote:app.remote.close(disconnect=False)
                if app.session:app.session.running=False
            for server in servers:server.shutdown();server.server_close()
            for r in runtimes.values():r.close()
