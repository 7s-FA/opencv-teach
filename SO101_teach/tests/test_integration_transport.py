from types import SimpleNamespace
from unittest.mock import Mock,patch
from pathlib import Path
import json,unittest
from tests import test_integration as fixtures
from so101_teach.integration_transport import IntegrationBridge


class IntegrationTransportTests(unittest.TestCase):
    setUp=fixtures.IntegrationTests.setUp
    def test_bridge_dispatches_on_gui_poll_and_prevents_duplicate_receiver(self):
        path=self.data/'recipes.json';path.write_text(json.dumps(self.config))
        manager=SimpleNamespace(apps=self.apps,data_dir=self.data,root=Mock())
        with patch.dict('os.environ',{'ROS_LOCALHOST_ONLY':'1'}):bridge=IntegrationBridge(manager,path,domain=197)
        try:
            with self.assertRaisesRegex(ValueError,'이미 실행'):IntegrationBridge(manager,path,domain=197)
            bridge.inbox.put({'kind':'command','arm':'arm2','command':'A'});bridge.poll()
            self.apps['arm2'].execute_episode.assert_called_once()
            self.assertIsNotNone(bridge.coordinator.active)
        finally:bridge.close()
        self.apps['arm2'].motion_request.assert_called_once_with('hold')
        self.assertIsNotNone(bridge.process.returncode)
