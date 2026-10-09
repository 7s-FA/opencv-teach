import unittest,time
from concurrent.futures import Future
from copy import deepcopy
from unittest.mock import patch
from . import test_ui as fixtures

class WorkflowAuditTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera

    def test_camera_jig_selection_does_not_change_teaching_jig(self):
        a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed();a.select_camera_jig(other['id'])
        self.assertEqual(a.selected_jig_id(),'pallet')
        self.assertEqual(a.active_jig,other['id'])
        self.assertFalse(hasattr(a,'reread_teaching_reference'))

    def test_geometry_change_invalidates_previous_camera_measurement(self):
        a=self.app;self.fake_jig_camera();self.assertIsNotNone(a.current_jig_reference())
        a.table_z.set('2.4');a.save_model()
        self.assertIsNone(a.current_jig_reference())
        self.fake_jig_camera();self.assertIsNotNone(a.current_jig_reference())

    def test_camera_calibration_result_waits_for_episode_to_finish(self):
        a=self.app;s=a.settings;called=[];f=Future();f.set_result({'value':1})
        s.job=(f,lambda value:called.append(value));a.pending_execution={'action':'play'}
        s.poll();self.assertEqual(called,[]);self.assertIsNotNone(s.job)
        a.pending_execution=None;s.poll();self.assertEqual(called,[{'value':1}]);self.assertIsNone(s.job)

    def test_jig_copy_cannot_mutate_catalog_during_execution(self):
        a=self.app;s=a.settings;before=deepcopy(a.catalog.items);a.set_pose_frozen(True)
        with self.assertRaises(ValueError):s.copy_jig()
        self.assertEqual(a.catalog.items,before)

    def test_inflight_detection_cannot_restore_cleared_measurement(self):
        import numpy as np
        d=self.app.detector
        def interrupted(*args,**kwargs):
            d.clear()
            return {'selected':None,'candidates':[],'status':'not_found'}
        with patch('so101_teach.vision_service.detect',side_effect=interrupted):
            result=d.process(np.zeros((720,1280,3),np.uint8))
        self.assertEqual(result['status'],'settings_changed')
        self.assertEqual(result['by_jig'],{})

    def test_status_text_does_not_resize_camera_or_teaching_body(self):
        a=self.app;a.show_page('camera');self.root.update()
        before=(a.camera_canvas.winfo_width(),a.camera_canvas.winfo_height())
        a.camera_status.set('지그 측정 완료 · 위치와 각도 확인\n로봇 좌표는 모델 계산값입니다.')
        a.message.set('측정 완료\n선택한 지그 기준을 저장했습니다.');self.root.update()
        self.assertEqual(before,(a.camera_canvas.winfo_width(),a.camera_canvas.winfo_height()))

class MotionSequenceAuditTests(unittest.TestCase):
    def test_random_tick_episodes_are_monotonic_bounded_and_reach_end(self):
        import random
        from .test_motion import MotionTests
        from so101_teach.domain import JOINTS
        fixture=MotionTests();fixture.setUp()
        try:
            fixture.s.arm(fixture.bus,fixture.snapshot());rng=random.Random(924)
            for rate in (300,350,400):
                fixture.s.set_speed(rate)
                targets=[{n:fixture.ref.middle[n]+rng.randint(-70,70) for n in JOINTS} for _ in range(12)]
                fixture.s.request('play',targets);fixture.s.on_snapshot(fixture.bus,fixture.snapshot())
                previous=fixture.s.last_goals.copy();iterations=0
                while fixture.s.state=='MOVING' and iterations<10000:
                    index=fixture.s.index;goal=targets[index]
                    fixture.clock+=.02;fixture.s.heartbeat=fixture.clock;fixture.s.on_snapshot(fixture.bus,fixture.snapshot());iterations+=1
                    for n in JOINTS:
                        now=fixture.s.last_goals[n]
                        self.assertLessEqual(abs(now-previous[n]),fixture.s.max_step_ticks)
                        self.assertGreaterEqual((now-previous[n])*(goal[n]-previous[n]),0)
                        self.assertTrue(fixture.cal.motors[n].low<=now<=fixture.cal.motors[n].high)
                    previous=fixture.s.last_goals.copy()
                self.assertEqual(fixture.s.state,'HOLD');self.assertEqual(fixture.s.last_goals,targets[-1])
            fixture.s.release(fixture.bus);self.assertTrue(all(row['Torque_Enable']==0 for row in fixture.values.values()))
        finally:fixture.doCleanups()
