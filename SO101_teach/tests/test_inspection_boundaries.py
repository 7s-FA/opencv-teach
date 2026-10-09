from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from tests.fixtures import load_profile
from so101_teach.domain import EpisodeStore
from so101_teach.ui import App
from so101_teach.episode_execution import InspectedExecution
from so101_teach.episode_transfer import episode_snapshot

CHECK=dict(station='linear',target='2',expected='housing_seated',timeout_seconds=5)

class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.profile,cal,ref=load_profile();self.store=EpisodeStore(Path('/unused'),cal)
        self.episode=self.store.new('B');self.episode['product_type']='B'
        self.episode['steps']=[self.store.step(ref.middle,name) for name in ('검사','회피')]
    def test_partial_move_does_not_validate_unselected_completion_targets(self):
        self.episode['completion_events']={'LOWER':self.episode['steps'][0]['id']}
        app=SimpleNamespace(live_adjust=SimpleNamespace(dispatching=False,stop=Mock()),episode=self.episode,store=self.store,pending_execution=None,camera_task=None,session=None,remote_plan_job=None,
                            stop_preview=Mock(side_effect=RuntimeError('VALIDATION_PASSED')))
        before=deepcopy(self.episode)
        with self.assertRaisesRegex(RuntimeError,'VALIDATION_PASSED'):
            App.prepare_execution(app,'move',[self.episode['steps'][1]])
        self.assertEqual(self.episode,before)
    def test_pc_linear_worker_reads_current_controller_state(self):
        import so101_teach.episode_execution as module
        app=SimpleNamespace(start_camera=Mock(),notice=Mock(),data_dir=Path('/unused'),profile={},catalog=None,workcell_preview={})
        run=object.__new__(InspectedExecution);run.app=app;run.product='B';run.fixed_jigs={}
        with patch.object(module,'FrameInspection') as worker:
            run.make_worker({'name':'중단','inspection':CHECK})
        self.assertTrue(callable(worker.call_args.kwargs.get('linear_state_reader')))
    def test_export_allows_check_on_last_normal_step(self):
        self.episode['steps'][-1]['inspection']=CHECK
        app=SimpleNamespace(profile=self.profile,episode=self.episode,store=self.store,episode_name=SimpleNamespace(get=lambda:'B'))
        self.assertEqual(episode_snapshot(app)['steps'][-1]['inspection'],CHECK)
