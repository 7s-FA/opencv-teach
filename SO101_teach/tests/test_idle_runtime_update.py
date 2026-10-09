import json,unittest,signal
from types import SimpleNamespace
from unittest.mock import patch,Mock
from so101_teach.remote_server import stop_stale_idle_server
from so101_teach.domain import ROOT

class IdleUpdateTests(unittest.TestCase):
    def response(self,value):
        from io import BytesIO
        return BytesIO(json.dumps({'ok':True,'value':value}).encode())
    def test_active_device_or_other_runtime_is_never_stopped(self):
        current={'instance':'old','version':'old-source','token':'test'}
        for key in ('camera','follower','leader','calibration','instance'):
            state={'instance':'old','detached':True};camera={'running':False}
            if key=='camera':camera['running']=True
            elif key=='instance':state['instance']='new'
            else:state[key]={'running':True}
            with patch('urllib.request.urlopen',side_effect=[self.response(state),self.response(camera)]),patch('so101_teach.remote_server.os.kill') as kill,self.assertRaises(RuntimeError):stop_stale_idle_server(current,8765)
            kill.assert_not_called()
    def test_only_verified_detached_idle_process_can_be_stopped(self):
        current={'instance':'old','version':'old-source','token':'test'};state={'instance':'old','detached':True};info={**current,'pid':123,'port':8765}
        with patch('urllib.request.urlopen',side_effect=[self.response(state),self.response({'running':False})]),patch('so101_teach.remote_server.read_json',return_value=info),patch('pathlib.Path.read_bytes',return_value=b'python\0-m\0so101_teach.remote_server\0'),patch('pathlib.Path.resolve',return_value=ROOT),patch('so101_teach.remote_server.os.kill') as kill:
            stop_stale_idle_server(current,8765);kill.assert_called_once_with(123,signal.SIGTERM)
