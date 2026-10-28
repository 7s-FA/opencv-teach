import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from tests import test_ui


class YoloUITests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown

    def open_panel(self):
        a=self.app;a.show_page('camera');a.camera_tabs.select(a.camera_yolo);self.root.update()
        return a,a.camera_yolo

    def camera(self,a,at=None):
        at=time.monotonic() if at is None else at
        frame=np.zeros((48,64,3),dtype=np.uint8)
        a.camera=SimpleNamespace(running=True,error=None,preview_observation=(frame,{'selected':None},at),
                                 observation=None,set_mode=Mock(),close=lambda:None)
        a.camera_manual_off=False;a.camera_sleeping=False
        return frame,at

    def test_yolo_view_disables_only_background_opencv_and_keeps_measurement_priority(self):
        a,p=self.open_panel();self.camera(a);a.update_camera_mode()
        self.assertFalse(a.camera.set_mode.call_args.kwargs['processing_enabled'])
        a.pending_execution={'task':'measurement'};a.update_camera_mode()
        self.assertTrue(a.camera.set_mode.call_args.kwargs['processing_enabled'])
        a.pending_execution=None;a.camera_tabs.select(0);self.root.update();a.update_camera_mode()
        self.assertTrue(a.camera.set_mode.call_args.kwargs['processing_enabled'])

    def test_old_result_after_camera_gap_is_discarded(self):
        a,p=self.open_panel();frame,at=self.camera(a,time.monotonic()-2)
        p.active=True;p.source=a.camera;p.worker=Mock();p.worker.poll.return_value=None
        p.update_frame(time.monotonic());cutoff=p.valid_after
        a.camera.preview_observation=(frame,None,time.monotonic())
        p.worker.poll.return_value={'kind':'result','frame_at':cutoff-.1}
        p.update_frame(time.monotonic())
        self.assertIsNone(p.result);self.assertEqual(p.table.get_children(),())

    def test_tab_switch_cancels_worker_and_clears_display(self):
        a,p=self.open_panel();self.camera(a);p.active=True;p.source=a.camera;p.worker=Mock()
        p.result={'frame_at':time.monotonic()};p.table.insert('','end',values=('이전 객체','99%'))
        a.camera_tabs.select(0);self.root.update()
        self.assertFalse(p.active);p.worker.close.assert_called();self.assertIsNone(p.result)
        self.assertEqual(p.table.get_children(),())

    def test_yolo_result_never_writes_jig_or_motion_state(self):
        from copy import deepcopy
        a,p=self.open_panel();frame,at=self.camera(a)
        episode=deepcopy(a.episode);target=deepcopy(a.target);catalog=deepcopy(a.catalog.items)
        p.active=True;p.source=a.camera;p.worker=Mock();p.worker.submit.return_value=False
        p.worker.poll.return_value={'kind':'result','frame_at':at,'image':frame,'raw':frame,
                                   'total_ms':500.,'inference_ms':400.,'confidence':.25,
                                   'detections':[{'label':'A 하단','confidence':.95}]}
        p.update_frame(time.monotonic())
        self.assertEqual(a.episode,episode);self.assertEqual(a.target,target);self.assertEqual(a.catalog.items,catalog)
        self.assertIsNone(a.camera.observation);self.assertEqual(len(p.table.get_children()),1)
        self.assertEqual(a.camera_results(),{})

    def test_inference_error_is_visible_and_can_be_restarted(self):
        a,p=self.open_panel();self.camera(a);p.active=True;p.source=a.camera;p.worker=Mock()
        p.worker.poll.return_value={'kind':'error','error':'모델 손상'}
        p.update_frame(time.monotonic())
        self.assertFalse(p.active);self.assertIn('모델 손상',p.status.get())
        self.assertFalse(p.start_button.instate(['disabled']));self.assertTrue(p.save_button.instate(['disabled']))

    def test_stale_and_duplicate_captures_are_rejected(self):
        a,p=self.open_panel();frame,at=self.camera(a)
        p.active=True;p.source=a.camera;p.result={'frame_at':at-6}
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()
        p.result={'frame_at':at};p.saved_at=at
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()

    def test_camera_error_and_changed_source_cannot_save_old_frame(self):
        a,p=self.open_panel();frame,at=self.camera(a)
        p.active=True;p.source=a.camera;p.result={'frame_at':at}
        a.camera.error='연결 끊김'
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()
        a.camera.error=None;p.source=object()
        with self.assertRaisesRegex(ValueError,'최신'):p.save_capture()

    def test_model_and_threshold_changes_clear_and_restart_active_worker(self):
        a,p=self.open_panel();self.camera(a);p.worker=Mock()
        with patch.object(p,'runtime_options',return_value=('fixture',{})):
            p.start();p.result={'frame_at':time.monotonic()}
            p.model_choice.set('Pi · YOLO26s');p.model_changed()
            self.assertTrue(p.active);self.assertIsNone(p.result)
            self.assertIn('YOLO26s',p.model_title.get());self.assertEqual(p.worker.start.call_count,2)
            p.confidence.set('90');p.threshold_changed()
            self.assertTrue(p.active);self.assertEqual(p.worker.start.call_count,3)
        self.assertEqual(float(p.threshold['from']),50);self.assertEqual(float(p.threshold['to']),90)
        self.assertFalse(p.threshold.instate(['readonly']))

    def test_percent_keyboard_edit_invalidates_result_and_only_commits_valid_range(self):
        a,p=self.open_panel();self.camera(a);p.worker=Mock()
        self.assertEqual(p.confidence.get(),'50');self.assertEqual(p.applied_confidence,.5)
        with patch.object(p,'runtime_options',return_value=('fixture',{})):
            p.start();p.result={'frame_at':time.monotonic()}
            p.threshold.delete(0,'end');p.threshold.insert(0,'91')
            self.assertFalse(p.active);self.assertIsNone(p.result)
            self.assertFalse(p.threshold_changed());self.assertEqual(p.applied_confidence,.5)
            self.assertTrue(p.threshold.instate(['invalid']));self.assertIn('50~90',p.status.get())
            p.threshold.delete(0,'end');p.threshold.insert(0,'75.5');self.assertEqual(p.confidence.get(),'')
            p.threshold.insert(0,'abc');self.assertEqual(p.confidence.get(),'')
            p.threshold.insert(0,'76');p.threshold_changed()
            self.assertTrue(p.active);self.assertEqual(p.applied_confidence,.76)
            starts=p.worker.start.call_count;p.threshold_changed()
            self.assertEqual(p.worker.start.call_count,starts)

    def test_fps_measures_completed_results_and_resets_on_stop(self):
        a,p=self.open_panel();frame,at=self.camera(a)
        p.active=True;p.source=a.camera;p.worker=Mock();p.worker.submit.return_value=False
        for completed in (at-.5,at):
            p.worker.poll.return_value={'kind':'result','frame_at':at,'finished_at':completed,'image':frame,'raw':frame,
                                       'total_ms':120.,'inference_ms':100.,'confidence':.5,'detections':[]}
            p.update_frame(time.monotonic())
        self.assertAlmostEqual(p.fps,2.);self.assertIn('FPS 2.0',p.metrics.get());self.assertIn('100.0ms',p.metrics.get())
        p.stop();self.assertIsNone(p.fps);self.assertEqual(len(p.completed_times),0)

    def test_active_yolo_requests_ten_fps_once_and_measurement_stays_prioritized(self):
        a,p=self.open_panel();self.camera(a);p.active=True
        a.camera.set_mode.reset_mock();a.update_camera_mode()
        a.camera.set_mode.assert_called_once_with(processing_enabled=False,preview_fps=10)
        a.pending_execution={'task':'measurement'};a.update_camera_mode()
        self.assertTrue(a.camera.set_mode.call_args.kwargs['processing_enabled'])
        a.pending_execution=None;p.active=False

    def test_four_models_route_to_their_own_runtime_and_weights(self):
        import json,tempfile
        from pathlib import Path
        from so101_teach.yolo_ui import MODEL_CHOICES
        a,p=self.open_panel()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);paths={key:root/key for key in ('pc','sam3','pi','pi_n')}
            for path in paths.values():
                path.mkdir();(path/'manifest.json').write_text('{"files": {}}')
            python=root/'python';python.touch()
            config={'pc_python':str(python),'sam3_python':str(python),'pi_python':'/pi/python',
                    'pi_app_dir':'/pi/app','pi_model':'models/s','pi_n_model':'models/n'}
            (a.data_dir/'yolo-runtime.json').write_text(json.dumps(config))
            with patch('so101_teach.yolo_ui.MODEL_PATHS',paths),patch('so101_teach.pi_connection.load'),patch('so101_teach.pi_connection.validate',return_value={'host':'pi'}):
                for label,key in MODEL_CHOICES.items():
                    p.model_choice.set(label);p.update_model_label();model,options=p.runtime_options()
                    self.assertEqual(options['backend'],'sam3' if key=='sam3' else 'tensorrt' if key=='pc' else 'ncnn')
                    self.assertEqual('remote' in options,key in ('pi','pi_n'))
                    self.assertEqual(model,config['pi_n_model' if key=='pi_n' else 'pi_model'] if key in ('pi','pi_n') else paths[key])
                    if key=='sam3':self.assertIn('BF16',p.model_detail.get());self.assertNotIn('FP16',p.model_detail.get())

    def test_unavailable_n_does_not_start_camera_or_worker(self):
        from pathlib import Path
        a,p=self.open_panel();p.worker=Mock();a.start_camera=Mock()
        with patch.dict('so101_teach.yolo_ui.MODEL_PATHS',pi_n=Path('/nonexistent/so101-test-n-model')):
            p.model_choice.set('Pi · YOLO26n');p.model_changed()
            self.assertIn('완료 후 사용 가능',p.model_detail.get())
            with self.assertRaisesRegex(ValueError,'준비 중'):p.start()
        a.start_camera.assert_not_called();p.worker.start.assert_not_called();self.assertFalse(p.active)
