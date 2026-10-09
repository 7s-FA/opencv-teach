from copy import deepcopy
import json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import cv2
from so101_teach.domain import ROOT
from so101_teach.inspection_tracking import PalletReference
from so101_teach.episode_inspection_runtime import FrameInspection,FrameInspections
from so101_teach.episode_inspection import fixed_jig_results,inspection_jig_ids
from so101_teach.camera_lifecycle import CameraLifecycle

class FixedInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder=ROOT/'tests/fixtures/inspection-all';cls.cfg=json.loads((cls.folder/'fixture.json').read_text())
        cls.frame=cv2.imread(str(cls.folder/'complete.jpg'))
        observation=PalletReference(cls.folder/'reference').locate(cls.frame,cls.cfg['profile'],cls.cfg['catalog']['pallet'])
        if observation is None:
            from so101_teach.occupied_pallet import locate
            observation=locate(cls.frame,cls.cfg['catalog']['pallet'],cls.cfg['profile'])
        metric=observation['selected']['metric'];cls.poses={'pallet':{'pose':[*metric['center_xy_mm'],metric['yaw_deg']],'symmetry_deg':90}}
        cls.check=dict(station='finished',target='pallet',expected='cap_added',timeout_seconds=10)
    def run_worker(self,poses,*,group=False):
        catalog=SimpleNamespace(items=self.cfg['catalog'],mesh=Mock(side_effect=AssertionError('No jig mesh processing during execution')))
        with tempfile.TemporaryDirectory() as folder,patch('so101_teach.vision_service.MultiDetector',side_effect=AssertionError('No redetection')),patch('so101_teach.inspection_tracking.PalletReference',side_effect=AssertionError('No retracking')),patch('so101_teach.occupied_pallet.locate',side_effect=AssertionError('No relocation')):
            if group:w=FrameInspections(folder,self.cfg['profile'],catalog,self.cfg['placement'],[dict(id='s',name='완성품',inspection=self.check,inspection_product='B')],fixed_jigs=poses)
            else:w=FrameInspection(folder,self.cfg['profile'],catalog,self.cfg['placement'],self.check,'B',fixed_jigs=poses)
            try:
                if poses:poses['pallet']['pose'][0]+=100 # the execution snapshot is immutable
                w.submit(self.frame,time.monotonic());until=time.monotonic()+5
                while w.result is None and time.monotonic()<until:time.sleep(.01)
                self.assertIsNotNone(w.result);self.assertNotIn('error',w.result)
                return w.result['results']['s'] if group else w.result
            finally:w.close()
    def test_loaded_pallet_is_inspected_without_detection_or_tracking(self):
        row=self.run_worker(deepcopy(self.poses))['row'];self.assertEqual(row['state'],'cap_added');self.assertEqual(row['product'],'B');self.assertEqual(row['quality'],'normal')
    def test_startup_group_reuses_the_same_confirmed_positions(self):
        row=self.run_worker(deepcopy(self.poses),group=True)['row'];self.assertEqual(row['state'],'cap_added')
    def test_missing_snapshot_never_falls_back_to_a_new_pose(self):
        result=self.run_worker({});self.assertIsNone(result['row']);self.assertIn('확정 지그 위치 없음',result['reason'])
    def test_camera_processing_is_off_while_episode_inspections_run(self):
        camera=Mock();app=SimpleNamespace(camera=camera,camera_task=None,pending_execution=None,jig_updates_paused=False,detector=SimpleNamespace(frozen=False),inspection_run=SimpleNamespace(busy=True))
        CameraLifecycle.update_camera_mode(app);camera.set_mode.assert_called_once_with(processing_enabled=False,preview_fps=10)
        self.assertTrue(CameraLifecycle.camera_needed(app))
    def test_inspection_targets_are_measured_even_when_not_motion_jigs(self):
        self.assertEqual(inspection_jig_ids([{'inspection':self.check}]),{'pallet'})
        pose={'carrier':{'pose':[10,20,30],'symmetry_deg':360,'mesh_yaw_offset_deg':180}}
        result=fixed_jig_results(pose)['carrier']['selected'];self.assertTrue(result['orientation_verified']);self.assertEqual(result['metric']['center_xy_mm'],[10,20]);self.assertEqual(result['metric']['mesh_yaw_offset_deg'],180)
