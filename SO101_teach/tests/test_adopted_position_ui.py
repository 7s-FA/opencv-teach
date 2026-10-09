import unittest,time
from copy import deepcopy
import numpy as np
from . import test_ui as fixtures
from . import test_camera_overview as camera_fixtures
from so101_teach.domain import read_json

class AdoptedPositionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    camera=camera_fixtures.CameraOverviewTests.camera
    def held_scene(self):
        frame,data=self.camera();a=self.app;data=deepcopy(data);now=time.monotonic()
        for r in data['by_jig'].values():r.update(pose_held=True,pose_measured_at=now-2)
        for key in data['live_by_jig']:data['live_by_jig'][key]={'selected':None,'candidates':[],'status':'not_found'}
        a.camera.observation=(frame,data,now);a.show_adoption.set(True);a.toggle_adoption_display();return frame,data
    def test_missing_live_detection_keeps_accepted_outline_and_capture_metadata(self):
        frame,data=self.held_scene();a=self.app;snapshot=a.camera_display_snapshot
        self.assertEqual(a.camera_jig_status.item('pallet','values')[1],'미검출')
        accepted=snapshot['result']['adopted_by_jig'];self.assertEqual(accepted['pallet']['selected']['center_px'],data['by_jig']['pallet']['selected']['center_px'])
        cx,cy=np.rint(accepted['pallet']['selected']['center_px']).astype(int)
        self.assertTrue(np.any(snapshot['base_image'][cy-12:cy+12,cx-12:cx+12]!=frame[cy-12:cy+12,cx-12:cx+12]))
        path=a.save_camera();metadata=read_json(path.parent/'metadata'/path.with_suffix('.json').name)
        self.assertIn('pallet',metadata['detection']['adopted_by_jig']);self.assertIsNone(metadata['detection']['by_jig']['pallet']['selected'])
    def test_new_live_position_does_not_move_held_position_and_off_hides_it(self):
        frame,data=self.held_scene();a=self.app;held=deepcopy(data['by_jig']['pallet']['selected']);live=deepcopy(held)
        for key in ('quad','outline_px','axes_px'):
            if key in live:live[key]=(np.array(live[key])+[85,0]).tolist()
        live['center_px']=(np.array(live['center_px'])+[85,0]).tolist();data['live_by_jig']['pallet']={'selected':live,'candidates':[live],'status':'shape_match'}
        a.draw_camera_frame(time.monotonic());snapshot=a.camera_display_snapshot['result']
        self.assertEqual(snapshot['adopted_by_jig']['pallet']['selected']['center_px'],held['center_px']);self.assertEqual(snapshot['by_jig']['pallet']['selected']['center_px'],live['center_px'])
        a.show_adoption.set(False);a.toggle_adoption_display();self.assertNotIn('adopted_by_jig',a.camera_display_snapshot['result'])
        self.assertEqual(data['by_jig']['pallet']['selected'],held)
    def test_expired_is_removed_frozen_is_kept_and_stale_video_clears(self):
        frame,data=self.held_scene();a=self.app
        for r in data['by_jig'].values():r['pose_measured_at']=time.monotonic()-20
        a.draw_camera_frame(time.monotonic());self.assertEqual(a.camera_display_snapshot['result']['adopted_by_jig'],{})
        data['by_jig']['pallet']['pose_frozen']=True;a.draw_camera_frame(time.monotonic());self.assertIn('pallet',a.camera_display_snapshot['result']['adopted_by_jig'])
        a.camera.observation=(frame,data,time.monotonic()-3);a.draw_camera_frame(time.monotonic());self.assertIsNone(a.camera_display_snapshot)
    def test_all_and_single_held_overlays_render(self):
        from PIL import ImageGrab
        frame,data=self.held_scene();a=self.app
        def shot(name):
            a.camera.observation=(frame,data,time.monotonic());self.root.update();self.root.after_cancel(a.job);a.poll();a.draw_camera_frame(time.monotonic());self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/held-position-'+name+'.png')
        shot('all');a.camera_jig_choice.current(1);a.select_camera_jig();shot('single')
        self.assertEqual(set(a.camera_display_snapshot['result']['adopted_by_jig']),{'pallet'})
