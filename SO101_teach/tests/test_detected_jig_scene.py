"""Detected surroundings stay visible without rewriting teaching references."""
import time,unittest
from copy import deepcopy
from types import SimpleNamespace
from tests import test_taught_scene as fixtures
from so101_teach.domain import Snapshot


class DetectedJigSceneTests(unittest.TestCase):
    setUp=fixtures.TaughtSceneTests.setUp
    def tearDown(self):
        self.app.session=None
        fixtures.TaughtSceneTests.tearDown(self)
    fake_jig_camera=fixtures.TaughtSceneTests.fake_jig_camera
    teach=fixtures.TaughtSceneTests.teach
    render_request=fixtures.TaughtSceneTests.render_request

    def add_detected_carrier(self):
        a=self.app;other=deepcopy(a.catalog.items['pallet']);other.update(id='carrier',name='운반용 지그')
        a.catalog.items['carrier']=other
        frame,result,at=a.camera.observation;carrier=deepcopy(result)
        carrier['selected']['metric'].update(center_xy_mm=[80,90],yaw_deg=32.,mesh_yaw_offset_deg=180.)
        a.camera.observation=(frame,{'by_jig':{'pallet':result,'carrier':carrier}},at)

    def live(self):
        a=self.app;a.mode='live'
        sample=Snapshot('follower',a.reference.middle,{},time.monotonic(),time.time(),a.calibration.sha256,True,'test')
        a.session=SimpleNamespace(running=True,error=None,latest=sample,state='HOLD')

    def test_step_shows_detected_jigs_without_overwriting_saved_basis(self):
        a=self.teach();self.add_detected_carrier();before=deepcopy(a.episode)
        jigs={j['id']:j for j in self.render_request(a)['jigs']}
        self.assertEqual(jigs['pallet']['pose'],[250,-90,12])
        self.assertEqual(jigs['carrier']['pose'],[80,90,32.])
        self.assertEqual(jigs['carrier']['mesh_yaw_offset_deg'],180.)
        self.assertEqual(a.episode,before)
        self.assertIn('운반용 지그: 탐지 확정',a.preview_jig_caption.get())
        self.assertIn('탐지 지그 보정 자세',a.preview_jig_caption.get())

    def test_previous_execution_snapshot_does_not_override_step_detection(self):
        a=self.teach();self.add_detected_carrier();a.measured_target_active=True
        a.measured_jig_display={'pallet':deepcopy(a.current_jig_reference('pallet'))}
        a.measured_jig_display['pallet']['pose']=[10,20,30]
        jigs={j['id']:j for j in self.render_request(a)['jigs']}
        self.assertEqual(jigs['pallet']['pose'],[250,-90,12])
        self.assertEqual(jigs['carrier']['pose'],[80,90,32.])

    def test_live_uses_current_detection_without_changing_execution_snapshot(self):
        a=self.teach();self.add_detected_carrier();self.live()
        a.measured_jig_display={'pallet':deepcopy(a.episode['steps'][0]['jig_reference'])}
        a.measured_jig_display['pallet']['pose']=[10,20,30]
        before=deepcopy(a.measured_jig_display)
        jigs={j['id']:j for j in self.render_request(a)['jigs']}
        self.assertEqual(jigs['pallet']['pose'],[250,-90,12])
        self.assertEqual(jigs['carrier']['pose'],[80,90,32.])
        self.assertEqual(a.measured_jig_display,before)

    def test_expired_detection_does_not_invent_a_missing_reference(self):
        a=self.teach();self.add_detected_carrier();frame,result,at=a.camera.observation
        for item in result['by_jig'].values():item['pose_measured_at']=time.monotonic()-a.pose_latch.seconds-1
        a.camera.observation=(frame,result,time.monotonic()-a.pose_latch.seconds-1)
        jigs={j['id']:j for j in self.render_request(a)['jigs']}
        self.assertEqual(jigs['pallet']['pose'],[228,-138,67])
        self.assertIsNone(jigs['carrier']['pose'])

    def test_live_without_current_detection_explicitly_retains_execution_basis(self):
        a=self.teach();self.live();a.camera=None
        a.measured_jig_display={'pallet':deepcopy(a.episode['steps'][0]['jig_reference'])}
        self.assertEqual(self.render_request(a)['jigs'][0]['pose'],[228,-138,67])
        self.assertIn('실행 기준',a.preview_jig_caption.get())
