import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_episode_cli import cli
from tests.test_work_monitor import monitor

class RejectionTests(unittest.TestCase):
    def test_connection_rejection_is_watched_once_without_overwriting_active_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'data').mkdir();path=root/'data/episode-cli-arm2.json';path.write_text('{"status":"A_RUNNING"}')
            with patch.object(cli,'ROOT',root),patch.object(cli,'load_job',return_value=(root,{}, {},400,120)),patch.object(cli.Link,'open',side_effect=RuntimeError('다른 PC 창이 Pi를 사용 중입니다.')),patch.object(cli,'control_server'),patch.object(cli.signal,'signal'):
                self.assertEqual(cli.main(['build_b','--no-ros']),1)
            reader=monitor.Events(root);events,errors=reader.read_new();self.assertFalse(errors);self.assertEqual(len(events),1)
            self.assertEqual(events[0]['status'],'B_REJECTED:다른 PC 창이 Pi를 사용 중입니다.')
            self.assertEqual(reader.read_new(),([],[]));self.assertEqual(json.loads(path.read_text())['status'],'A_RUNNING')
    def test_check_failure_does_not_create_execution_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(cli,'ROOT',root),patch.object(cli,'load_job',side_effect=ValueError('bad config')):
                self.assertEqual(cli.main(['build_b','--check','--no-ros']),1)
            self.assertFalse((root/'data').exists())
