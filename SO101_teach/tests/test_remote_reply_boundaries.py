"""Malformed RPC replies must not leave an uncertain actuator command reusable."""
import io,json,unittest
from unittest.mock import patch
from so101_teach.remote_client import RemoteLink

class RemoteReplyBoundaryTests(unittest.TestCase):
    def link(self):
        link=RemoteLink({'host':'localhost','user':'robot'});link.base='http://127.0.0.1:1';link.token='synthetic';link.lease='lease'
        return link
    def test_malformed_command_ack_latches_failure_and_cannot_be_resent(self):
        for raw in ('{','null','[]','{}','{"ok":"false","value":1}','{"ok":true}','{"ok":true,"value":NaN}'):
            with self.subTest(raw=raw):
                link=self.link()
                with patch('so101_teach.remote_client.urlopen',return_value=io.StringIO(raw)) as network:
                    with self.assertRaises(ConnectionError):link.rpc('command',{'action':'play','targets':[]})
                    self.assertTrue(link.stop.is_set());self.assertIsNotNone(link.error)
                    with self.assertRaises(ConnectionError):link.rpc('command',{'action':'play','targets':[]})
                    network.assert_called_once()
    def test_explicit_server_rejection_keeps_transport_usable(self):
        link=self.link()
        with patch('so101_teach.remote_client.urlopen',return_value=io.StringIO(json.dumps({'ok':False,'error':'STALE_CONTROL'}))):
            with self.assertRaisesRegex(ValueError,'STALE_CONTROL'):link.rpc('command',{'action':'play'})
        self.assertIsNone(link.error);self.assertFalse(link.stop.is_set())
