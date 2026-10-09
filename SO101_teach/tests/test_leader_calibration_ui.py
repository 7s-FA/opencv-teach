import unittest
from unittest.mock import patch
from types import SimpleNamespace
from copy import deepcopy
from . import test_ui as fixtures
from so101_teach.domain import read_json

class LeaderCalibrationUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_leader_can_start_without_existing_json_on_separate_port(self):
        s=self.app.settings;s.vars['cal_role'].set('리더');s.calibration_panel.sync_role();s.vars['cal_port'].set('/dev/leader-test');s.vars['leader_json'].set('')
        with patch('so101_teach.calibration.CalibrationWorker') as worker:
            s.start_calibration();args=worker.call_args.args
            self.assertEqual(args[0],'/dev/leader-test');self.assertTrue(args[2].name.startswith('leader-'));worker.return_value.start.assert_called_once()
        s.worker=None
    def test_leader_cannot_use_follower_port(self):
        s=self.app.settings;s.vars['cal_role'].set('리더');s.vars['cal_port'].set(s.value('follower_port'))
        with patch('so101_teach.calibration.CalibrationWorker') as worker:
            with self.assertRaisesRegex(ValueError,'달라야'):s.start_calibration()
            worker.assert_not_called()
    def test_first_leader_calibration_persists_without_changing_follower(self):
        a=self.app;s=a.settings;original=deepcopy(a.profile);s.calibration_role='leader';s.vars['cal_role'].set('리더')
        s.worker=SimpleNamespace(port='/dev/leader-test',destination=self.data/original['calibration_file'],running=False,close=lambda:None)
        s.apply_completed_calibration();saved=read_json(self.data/'profile.json')
        self.assertEqual(saved['port'],original['port']);self.assertEqual(saved['calibration_file'],original['calibration_file']);self.assertEqual(saved['model_reference'],original['model_reference'])
        self.assertEqual(saved['leader']['port'],'/dev/leader-test');self.assertTrue((self.data/saved['leader']['calibration_file']).exists());self.assertEqual(s.value('leader_port'),'/dev/leader-test')
        s.vars['mode'].set('리더 + 팔로워');s.save_profile();self.assertEqual(a.profile['mode'],'leader');self.assertIsNone(a.session)
    def test_role_change_clears_other_arm_file_and_measurements(self):
        s=self.app.settings;s.vars['cal_import'].set(s.value('follower_json'));s.vars['leader_json'].set('');s.vars['cal_role'].set('리더');s.calibration_panel.sync_role()
        self.assertEqual(s.value('cal_import'),'');self.assertEqual(s.calibration_panel.last_sample['points'],{})
    def test_missing_leader_setup_does_not_open_any_port(self):
        a=self.app;a.profile['mode']='leader';a.profile.pop('leader',None)
        with patch('so101_teach.ui.MotionSession.start') as follower,patch('so101_teach.ui.ReadOnlySession.start') as leader:
            with self.assertRaisesRegex(ValueError,'리더 설정'):a.toggle_connection()
            follower.assert_not_called();leader.assert_not_called()
        self.assertIsNone(a.session)
    def test_missing_mode_defaults_to_leader(self):
        from so101_teach.domain import atomic_json
        from tests.fixtures import load_profile
        p=read_json(self.data/'profile.json');p.pop('mode',None);atomic_json(self.data/'profile.json',p)
        self.assertEqual(load_profile(self.data)[0]['mode'],'leader')
        self.app.settings.new_robot();self.assertEqual(self.app.settings.value('mode'),'리더 + 팔로워')
