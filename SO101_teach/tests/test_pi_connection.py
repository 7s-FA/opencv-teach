import subprocess,tempfile,unittest,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from copy import deepcopy
from . import test_ui as fixtures
from so101_teach import pi_connection as pi

class PiConnectionTests(unittest.TestCase):
    def values(self,**kwargs):return {**pi.DEFAULTS,'host':'raspberrypi.local','user':'robot',**kwargs}
    def test_roundtrip_only_connection_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pi.load(tmp),pi.DEFAULTS)
            d=pi.save(tmp,self.values());self.assertEqual(pi.load(tmp),d)
            self.assertEqual([p.name for p in Path(tmp).iterdir()],['pi-connection.json'])
    def test_host_user_port_and_paths_reject_malformed_input(self):
        for values in ({'host':'-oProxyCommand=bad'},{'host':'pi;touch /tmp/bad'},{'host':'ssh://pi'},{'user':'u@pi'},{'port':0},{'port':65536},{'port':'2.2'},{'python':'python3;bad'},{'app_dir':'relative'},{'app_dir':'/a\nb'}):
            with self.subTest(values=values),self.assertRaises(ValueError):pi.validate(self.values(**values))
        for host in ('192.0.2.10','raspberrypi.local','::1'):self.assertEqual(pi.validate(self.values(host=host))['host'],host)
    def test_empty_configuration_can_be_saved_but_not_connected(self):
        pi.validate(pi.DEFAULTS)
        with self.assertRaises(ValueError):pi.validate(pi.DEFAULTS,True)
    def test_probe_uses_constant_read_only_command_and_strict_host_check(self):
        result=SimpleNamespace(returncode=0,stdout='SO101_SSH_OK\nLinux\naarch64\n',stderr='')
        with patch.object(pi.subprocess,'run',return_value=result) as run:
            got=pi.probe(self.values());args=run.call_args.args[0]
            self.assertEqual(args[-1],pi.PROBE_COMMAND);self.assertIn('StrictHostKeyChecking=yes',args);self.assertIn('BatchMode=yes',args)
            self.assertFalse(run.call_args.kwargs.get('shell',False));self.assertEqual(run.call_args.kwargs['timeout'],8)
            self.assertEqual(got,{'target':'robot@raspberrypi.local:22','system':'Linux / aarch64'})
    def test_probe_errors_distinguish_authentication_identity_and_timeout(self):
        for stderr,expected in [('Permission denied','인증'),('Host key verification failed','호스트 키'),('Connection refused','연결에 실패')]:
            with patch.object(pi.subprocess,'run',return_value=SimpleNamespace(returncode=255,stdout='',stderr=stderr)):
                with self.assertRaisesRegex(ValueError,expected):pi.probe(self.values())
        with patch.object(pi.subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',8)):
            with self.assertRaisesRegex(ValueError,'초과'):pi.probe(self.values())
    def test_optional_key_path_is_not_read_or_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            key=Path(tmp)/'test key';key.write_text('not a real key')
            with patch.object(pi.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='SO101_SSH_OK\nLinux\naarch64',stderr='')) as run:
                pi.probe(self.values(identity_file=str(key)));args=run.call_args.args[0]
                self.assertEqual(args[args.index('-i')+1],str(key));self.assertNotIn('not a real key',' '.join(args))

class PiSettingsTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_save_does_not_change_robot_episode_or_calibration(self):
        a=self.app;s=a.settings;before=deepcopy(a.profile);episode=deepcopy(a.episode)
        s.vars['pi_host'].set('pi.local');s.vars['pi_user'].set('robot');s.pi_panel.save()
        self.assertEqual(pi.load(self.data)['host'],'pi.local');self.assertEqual(a.profile,before);self.assertEqual(a.episode,episode)
    def test_probe_completes_asynchronously_without_connecting_robot(self):
        a=self.app;s=a.settings;p=s.pi_panel;s.vars['pi_host'].set('pi.local');s.vars['pi_user'].set('robot')
        with patch('so101_teach.pi_connection.probe',return_value={'target':'robot@pi.local:22','system':'Linux / aarch64'}),patch.object(a,'toggle_connection',side_effect=AssertionError('No motors')):
            p.probe();self.assertTrue(p.probe_btn.instate(['disabled']))
            end=time.monotonic()+2
            while p.job and time.monotonic()<end:p.poll();time.sleep(.01)
        self.assertIsNone(p.job);self.assertIn('SSH 접속 확인됨',p.status.get());self.assertIn('별도 확인',p.status.get());self.assertIsNone(a.session)
    def test_calibration_review_allows_save_without_verification_input(self):
        s=self.app.settings;p=s.calibration_panel
        self.assertNotIn('cal_verify',s.vars);self.assertFalse(hasattr(p,'verify'))
        p.state_changed('RANGE');self.assertTrue(p.save.instate(['disabled']))
        p.state_changed('VERIFY');self.assertTrue(p.save.instate(['!disabled']))
        with patch.object(s,'cal_command') as cmd:p.save.invoke();cmd.assert_called_once_with('save')
