import base64,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.error import HTTPError
import cv2,numpy as np
from so101_teach.remote_client import RemoteLink,RemoteCameraSession,CameraTransport

class CameraTransportTests(unittest.TestCase):
    def link(self):return RemoteLink({'host':'pi','user':'robot'})
    def frame(self):
        _,data=cv2.imencode('.jpg',np.zeros((20,30,3),np.uint8))
        return {'running':True,'generation':0,'image':base64.b64encode(data).decode(),'server_now':10.,'at':10.,'detection':{'selected':None,'candidates':[]},'error':None}
    def test_start_and_stop_never_wait_on_network_in_ui_thread(self):
        link=self.link();entered=threading.Event();release=threading.Event();calls=[]
        def rpc(method):
            calls.append(method)
            if method=='camera_start':entered.set();release.wait(2)
        link.camera_rpc=rpc;c=RemoteCameraSession(link);c.transport=Mock()
        began=time.monotonic();c.start();self.assertLess(time.monotonic()-began,.1);self.assertTrue(entered.wait(1))
        c.close();self.assertTrue(c.running);release.set();self.assertTrue(c.join(2));self.assertEqual(calls,['camera_start','camera_stop']);c.transport.request.assert_not_called()
    def test_short_timeout_clears_stale_frame_then_recovers_without_control_failure(self):
        link=self.link();link.camera_rpc=Mock();c=RemoteCameraSession(link);c.observation=('old',{},0);c.running=True;calls=[0]
        def request():
            calls[0]+=1
            if calls[0]==1:raise TimeoutError('brief wifi gap')
            self.assertIsNone(c.observation);self.assertTrue(c.recovering);c.stop.set();return self.frame()
        c.transport=SimpleNamespace(request=request,close=Mock());c.run()
        self.assertIsNone(link.error);self.assertIsNone(c.error);self.assertFalse(c.recovering);self.assertIsNotNone(c.observation)
        self.assertEqual([x.args[0] for x in link.camera_rpc.call_args_list],['camera_start','camera_stop'])
    def test_long_camera_outage_does_not_fail_robot_link(self):
        link=self.link();link.camera_rpc=Mock();c=RemoteCameraSession(link);clock=[0.]
        def request():clock[0]+=3.;raise TimeoutError('camera offline')
        c.transport=SimpleNamespace(request=request,close=Mock())
        with patch('so101_teach.remote_client.time.monotonic',side_effect=lambda:clock[0]):c.run()
        self.assertIn('camera offline',c.error);self.assertIsNone(link.error);self.assertFalse(link.stop.is_set());self.assertFalse(c.running)
    def test_authentication_error_is_not_retried(self):
        link=self.link();link.camera_rpc=Mock();c=RemoteCameraSession(link)
        c.transport=SimpleNamespace(request=Mock(side_effect=HTTPError('url',401,'unauthorized',{},None)),close=Mock());c.run()
        self.assertEqual(c.transport.request.call_count,1);self.assertIsNone(link.error);self.assertIsNotNone(c.error)
    def test_video_uses_separate_nonmultiplexed_tunnel_and_checks_instance(self):
        link=self.link();link.server_port=8765;link.instance='runtime';link.base='control';link.http=Mock(side_effect=[{'instance':'runtime'},self.frame()])
        proc=Mock();proc.poll.return_value=None
        with patch('so101_teach.remote_client.subprocess.Popen',return_value=proc) as popen:
            transport=CameraTransport(link);transport.request();args=popen.call_args.args[0]
            self.assertIn('ControlMaster=no',args);self.assertIn('ControlPath=none',args)
            self.assertNotEqual(transport.base,link.base)
            self.assertEqual([x.args[0] for x in link.http.call_args_list],['/health','/camera'])
            self.assertTrue(all(x.kwargs['base']==transport.base for x in link.http.call_args_list));transport.close();proc.terminate.assert_called_once()
    def test_camera_commands_cannot_send_motor_actions_or_fail_control(self):
        link=self.link();link.http=Mock(side_effect=TimeoutError('video command delay'))
        with self.assertRaises(ValueError):link.camera_rpc('command')
        link.http.assert_not_called()
        with self.assertRaises(TimeoutError):link.camera_rpc('camera_start')
        self.assertIsNone(link.error);self.assertFalse(link.stop.is_set());self.assertEqual(link.http.call_count,1)
    def test_slow_response_has_no_adaptive_backoff_and_duplicate_frame_not_retimestamped(self):
        link=self.link();link.camera_rpc=Mock();c=RemoteCameraSession(link);clock=[0.];waits=[];calls=[0];data=self.frame()
        def request():calls[0]+=1;clock[0]+=.3;return data
        def wait(seconds):
            waits.append(seconds);clock[0]+=seconds
            if calls[0]>=5:c.stop.set()
        c.stop.wait=wait;c.transport=SimpleNamespace(request=request,close=Mock())
        with patch('so101_teach.remote_client.time.monotonic',side_effect=lambda:clock[0]):c.run()
        self.assertEqual(waits,[.01]*5);self.assertEqual(c.frame_at,0.)
    def test_finished_acquisition_metadata_arrives_without_a_duplicate_image_or_new_timestamp(self):
        from copy import deepcopy
        link=self.link();link.camera_rpc=Mock();c=RemoteCameraSession(link);first=self.frame();second=deepcopy(first);second.pop('image');second['server_now']=13
        second['detection']={'selected':{'metric':{}},'pose_measured_at':12.5,'acquisition_completed_at':13,'acquisition_completed_attempts':1}
        responses=iter([first,second]);calls=[0]
        def request():calls[0]+=1;return next(responses)
        c.transport=SimpleNamespace(request=request,close=Mock())
        c.stop.wait=lambda _:c.stop.set() if calls[0]==2 else None
        with patch('so101_teach.remote_client.time.monotonic',side_effect=[10,10,13,13]):c.run()
        self.assertEqual(c.observation[2],10);self.assertEqual(c.observation[1]['pose_measured_at'],12.5)
        self.assertEqual(c.observation[1]['acquisition_completed_at'],13);self.assertIsNotNone(c.observation[1]['selected'])
