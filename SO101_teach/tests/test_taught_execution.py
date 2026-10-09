import time
import unittest
from copy import deepcopy
from unittest.mock import patch
from . import test_ui as fixtures
from so101_teach.domain import read_json


class TaughtExecutionTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def teach(self):
        a=self.app;self.fake_jig_camera();a.follow_jig.set(True)
        ticks=a.reference.middle.copy();ticks['shoulder_pan']+=4;a.apply_target(ticks);a.commit_target()
        a.selected=None;ticks['shoulder_pan']+=4;ticks['gripper']+=6;a.apply_target(ticks);a.commit_target()
        a.apply_target(a.reference.middle);a.add_safe_steps();a.save_episode();a.camera=None
        return a

    def test_raw_run_preserves_every_tick_and_reference_without_camera_or_ik(self):
        a=self.teach();before=deepcopy(a.episode)
        path=next((self.data/'episodes').glob('*.json'));saved=path.read_bytes()
        with patch.object(a,'start_camera',side_effect=AssertionError('No camera')),patch.object(a,'current_jig_reference',side_effect=AssertionError('No measurement')),patch('so101_teach.geometry.corrected_step',side_effect=AssertionError('No IK')),patch.object(a,'motion_request',return_value=42) as request:
            a.execute_taught_episode()
        request.assert_called_once_with('play',[s['ticks'] for s in before['steps']])
        self.assertEqual(a.episode,before);self.assertEqual(path.read_bytes(),saved)
        self.assertIsNone(a.pending_execution);self.assertEqual(a.page,'teach')
        report=read_json(next((self.data/'episode_runs'/a.episode['id']).glob('*.json')))
        self.assertEqual(report['execution_mode'],'taught_ticks');self.assertEqual(report['state'],'submitted')
        self.assertIsNone(report['measured_at']);self.assertEqual(report['steps'],before['steps'])
        self.assertTrue(all(not x['corrected'] for x in report['plan']))
        self.assertIsNone(report['groups'][0]['measured']);self.assertIn('측정·보정 없음',a.comparison_caption(report))

    def test_next_normal_run_still_measures_jig(self):
        a=self.teach()
        with patch.object(a,'motion_request',return_value=1):a.execute_taught_episode()
        with patch.object(a,'start_camera'):
            a.prepare_execution('play',deepcopy(a.episode['steps']))
        self.assertIsNotNone(a.pending_execution);self.assertEqual(a.page,'camera')

    def test_empty_and_pending_runs_cannot_dispatch(self):
        a=self.app
        with patch.object(a,'motion_request') as request:
            with self.assertRaises(ValueError):a.execute_taught_episode()
            self.teach();a.safe_entry={'request_id':1}
            with self.assertRaises(ValueError):a.execute_taught_episode()
            a.safe_entry=None;a.pending_execution={'action':'play'}
            with self.assertRaises(ValueError):a.execute_taught_episode()
            request.assert_not_called();a.pending_execution=None

    def test_missing_connection_does_not_report_submission(self):
        a=self.teach()
        with self.assertRaises(ValueError):a.execute_taught_episode()
        report=read_json(next((self.data/'episode_runs'/a.episode['id']).glob('*.json')))
        self.assertEqual(report['state'],'blocked');self.assertEqual(report['execution_mode'],'taught_ticks')

    def test_demo_completes_all_steps_and_safe_return_without_camera(self):
        a=self.teach();a.profile['mode']='demo'
        before=deepcopy(a.episode)
        def pump(condition):
            end=time.monotonic()+6
            while not condition() and time.monotonic()<end:self.root.update();time.sleep(.02)
            self.assertTrue(condition(),a.message.get())
        with patch('so101_teach.devices.new_bus',side_effect=AssertionError('No hardware')),patch.object(a,'start_camera',side_effect=AssertionError('No camera')):
            a.toggle_connection()
            try:
                pump(lambda:a.latest is not None);a.motion_request('arm');pump(lambda:a.session.state=='HOLD')
                a.execute_taught_episode();pump(lambda:not a.session.program_active.is_set() and a.session.state=='HOLD')
                self.assertEqual(len(a.last_plan),len(before['steps']))
                self.assertEqual(a.session.last_goals,before['steps'][-1]['ticks']);self.assertEqual(a.episode,before)
            finally:a.session.close();a.session.join(1)
