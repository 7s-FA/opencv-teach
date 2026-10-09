import time,unittest
from copy import deepcopy
from unittest.mock import patch
import cv2
import numpy as np
from . import test_ui as fixtures
from so101_teach.domain import ROOT,read_json
from so101_teach.vision import detect,annotate


class CameraCaptureTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown

    def camera(self):
        fixtures.UITests.fake_jig_camera(self)
        frame=cv2.imread(str(ROOT/'verification/pallet-current.jpg'))
        a=self.app;result=detect(frame,a.mesh_profile,profile=a.profile)
        self.assertIsNotNone(result['selected'])
        a.camera.observation=(frame,result,time.monotonic());a.roi=[.45,.35,.8,.8]
        a.show_page('camera');self.root.update();a.draw_camera_frame(time.monotonic())
        return frame,result

    def test_capture_is_exact_visible_frame_with_roi_even_if_new_frame_arrives(self):
        frame,result=self.camera();a=self.app;shown=a.camera_display_snapshot['image'].copy()
        a.camera.observation=(np.zeros_like(frame),{'selected':None,'candidates':[]},time.monotonic())
        path=a.save_camera()
        self.assertEqual(path.parent,self.data/'captures'/'화면 표시본');self.assertEqual(path.suffix,'.png')
        np.testing.assert_array_equal(cv2.imread(str(path)),shown)
        self.assertFalse(np.array_equal(frame,shown))
        np.testing.assert_array_equal(cv2.imread(str(path.parent.parent/'원본'/path.name)),frame)
        meta=read_json(path.parent.parent/'판정 기록'/path.with_suffix('.json').name)
        self.assertEqual(meta['roi'],a.roi);self.assertEqual(meta['detection']['selected']['center_px'],result['selected']['center_px'])
        self.assertEqual(list(path.parent.glob('*.json')),[])

    def test_overlay_numbers_use_right_up_coordinates_and_pixel_fallback(self):
        frame,result=self.camera();before=deepcopy(result)
        with patch('cv2.putText',wraps=cv2.putText) as draw:annotate(frame,result)
        labels=[c.args[1] for c in draw.call_args_list]
        m=result['selected']['metric'];self.assertIn(f"X {-m['center_xy_mm'][1]:.1f}  Y {m['center_xy_mm'][0]:.1f} mm",labels)
        self.assertIn(f"Angle {(m['yaw_deg']+90)%m.get('symmetry_deg',90):.1f} deg",labels)
        self.assertEqual(result,before)
        result=deepcopy(result);result['selected']['metric']=None
        for c in result['candidates']:c['metric']=None
        with patch('cv2.putText',wraps=cv2.putText) as draw:annotate(frame,result)
        self.assertTrue(any('Image angle' in c.args[1] for c in draw.call_args_list))

    def test_roi_clear_updates_the_saved_image_and_metadata(self):
        self.camera();a=self.app;before=a.camera_display_snapshot['image'].copy()
        a.roi=None;a.draw_roi();path=a.save_camera()
        np.testing.assert_array_equal(cv2.imread(str(path)),a.camera_display_snapshot['base_image'])
        self.assertFalse(np.array_equal(before,cv2.imread(str(path))))
        self.assertIsNone(read_json(path.parent.parent/'판정 기록'/path.with_suffix('.json').name)['roi'])

    def test_capture_rejects_disconnected_or_stale_camera(self):
        self.camera();a=self.app;frame,result,at=a.camera.observation
        a.camera.observation=(frame,result,at-2)
        with self.assertRaises(ValueError):a.save_camera()
        self.assertFalse((self.data/'captures').exists())

    def test_edit_action_bottom_matches_left_column_at_both_sizes(self):
        a=self.app;a.show_page('teach')
        for size in ('1280x800','1180x760'):
            self.root.geometry(size);self.root.update()
            bottom=lambda w:w.winfo_rooty()+w.winfo_height()
            self.assertEqual(bottom(a.move_btn),bottom(a.execute_taught_btn),size)
            self.assertGreaterEqual(a.capture_btn.winfo_rooty(),bottom(a.sliders['gripper']))

    def test_switch_replaces_previous_overlay_immediately_without_waiting_for_frame(self):
        frame,first=self.camera();a=self.app
        other=a.catalog.duplicate('pallet');other['roi']=[.1,.1,.3,.3];a.catalog.save(other)
        a.catalog_changed();second={'selected':None,'candidates':[],'status':'not_found'}
        a.camera.observation=(frame,{'by_jig':{'pallet':first,other['id']:second}},time.monotonic())
        a.select_camera_jig('pallet');a.draw_camera_frame(time.monotonic())
        a.select_camera_jig(other['id'])
        self.assertEqual(a.camera_display_snapshot['jig_id'],other['id'])
        self.assertIsNone(a.camera_display_snapshot['result']['selected'])
        self.assertEqual(a.camera_display_snapshot['roi'],other['roi'])
        self.assertIn(other['name'],a.camera_status.get())
        path=a.save_camera();meta=read_json(path.parent.parent/'판정 기록'/path.with_suffix('.json').name)
        self.assertIsNone(meta['detection']['selected'])

    def test_switch_with_stale_frame_clears_previous_overlay(self):
        self.camera();a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed()
        frame,result,at=a.camera.observation;a.camera.observation=(frame,result,at-2)
        a.select_camera_jig(other['id'])
        self.assertIsNone(a.camera_display_snapshot)
        self.assertFalse(a.camera_canvas.find_withtag('image'))

    def test_rejected_switch_restores_choice_while_execution_frozen(self):
        self.camera();a=self.app;other=a.catalog.duplicate('pallet');a.catalog_changed()
        a.detector.freeze(True);a.camera_jig_choice.current(2)
        with self.assertRaises(ValueError):a.select_camera_jig()
        self.assertEqual(a.camera_jig_choice.current(),1)
        self.assertEqual(a.active_jig,'pallet')
