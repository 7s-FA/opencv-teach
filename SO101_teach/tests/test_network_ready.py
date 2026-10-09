import json
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch
from types import SimpleNamespace

DIR=Path(__file__).parents[1]/'integration';sys.path.insert(0,str(DIR))
import network_ready
from tests import test_action_contract as actions

LINK={'ifindex':3,'operstate':'UP','flags':['UP','LOWER_UP','MULTICAST'],
      'addr_info':[{'family':'inet','scope':'global','local':'192.0.2.10','valid_life_time':3600}]}
ROUTE={'dst':'192.0.2.10/24','scope':'link','prefsrc':'192.0.2.10','flags':[]}

class NetworkReadyTests(unittest.TestCase):
    def probe(self,link=LINK,routes=None):
        replies=[SimpleNamespace(stdout=json.dumps([link])),SimpleNamespace(stdout=json.dumps([ROUTE] if routes is None else routes))]
        with patch.object(network_ready.subprocess,'run',side_effect=replies):return network_ready.network_state('wlan0')
    def test_ready_link_and_connected_route_are_required(self):
        state,_=self.probe();self.assertEqual(state['addresses'],['192.0.2.10'])
        self.assertIsNone(self.probe(routes=[])[0]);self.assertIsNone(self.probe(routes=[{**ROUTE,'flags':['linkdown']}])[0])
        self.assertIsNone(self.probe(routes=[{**ROUTE,'prefsrc':'192.0.2.10'}])[0])
    def test_address_without_carrier_and_link_local_never_pass(self):
        link=deepcopy(LINK);link['flags'].remove('LOWER_UP');self.assertIsNone(self.probe(link)[0])
        for address in ('169.254.1.2','127.0.0.1','0.0.0.0'):
            link=deepcopy(LINK);link['addr_info'][0]['local']=address;self.assertIsNone(self.probe(link)[0])
    def test_failed_query_waits_instead_of_claiming_ready(self):
        with patch.object(network_ready.subprocess,'run',side_effect=FileNotFoundError):self.assertIsNone(network_ready.network_state('wlan0')[0])
        with self.assertRaises(ValueError):network_ready.network_state('--bad')
    def wait(self,probe,timeout=10):
        clock=[0.];events=[]
        def sleep(seconds):clock[0]+=seconds
        result=network_ready.wait_for_network(timeout=timeout,probe=lambda _:probe(clock[0]),clock=lambda:clock[0],sleep=sleep,emit=lambda text,**kwargs:events.append(text))
        return result,clock[0],events
    def test_late_address_waits_for_two_stable_seconds(self):
        state={'addresses':['192.0.2.10']}
        _,elapsed,events=self.wait(lambda now:(None,'주소 대기') if now<1.5 else (state,'준비됨'))
        self.assertEqual(elapsed,3.5);self.assertTrue(events[-1].startswith('NETWORK_READY'))
    def test_link_flap_or_address_change_restarts_stability_window(self):
        def probe(now):
            if now==1.5:return None,'링크 대기'
            return {'addresses':['192.0.2.10' if now<3 else '192.0.2.10']},'준비됨'
        _,elapsed,_=self.wait(probe);self.assertEqual(elapsed,5.)
    def test_unready_network_times_out_without_infinite_wait(self):
        with self.assertRaisesRegex(RuntimeError,'NETWORK_TIMEOUT'):self.wait(lambda now:(None,'경로 대기'),timeout=3)
        with self.assertRaises(ValueError):network_ready.wait_for_network(timeout=float('nan'))

class ServerStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):actions.ActionContractTests.setUpClass();cls.server=actions.ActionContractTests.module
    def test_timeout_does_not_initialize_ros_or_create_action_servers(self):
        with patch.object(self.server,'ROOT',DIR.parent),patch.object(network_ready,'wait_for_network',side_effect=RuntimeError('NETWORK_TIMEOUT')),patch.object(self.server.rclpy,'init',create=True) as init,patch.object(self.server,'ArmActionServer') as server:
            with self.assertRaisesRegex(RuntimeError,'NETWORK_TIMEOUT'):self.server.main(['--app-dir',str(DIR.parent)])
            init.assert_not_called();server.assert_not_called()
    def test_network_check_only_never_opens_ros_endpoints(self):
        with patch.object(self.server,'ROOT',DIR.parent),patch.object(network_ready,'wait_for_network') as wait,patch.object(self.server.rclpy,'init',create=True) as init,patch.object(self.server,'ArmActionServer') as server:
            self.server.main(['--app-dir',str(DIR.parent),'--network-check-only']);wait.assert_called_once();init.assert_not_called();server.assert_not_called()
    def test_production_start_waits_before_ros_initialization(self):
        order=[]
        with patch.object(self.server,'ROOT',DIR.parent),patch.object(network_ready,'wait_for_network',side_effect=lambda *args:order.append('network')),patch.object(self.server.rclpy,'init',side_effect=lambda **kwargs:order.append('ros'),create=True),patch.object(self.server.rclpy,'try_shutdown',create=True),patch.object(self.server,'ArmActionServer',return_value=Mock()),patch.object(self.server,'MultiThreadedExecutor',return_value=Mock()):
            self.server.main(['--app-dir',str(DIR.parent)])
        self.assertEqual(order,['network','ros'])
