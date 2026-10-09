import json,time,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from PIL import ImageGrab
import cv2,numpy as np
from . import test_ui as fixtures
from so101_teach.vision import annotate

class OutlineStyleTests(unittest.TestCase):
    def test_latest_outline_is_dashed_but_adopted_outline_remains_solid(self):
        c={'quad':[[20,20],[180,20],[180,120],[20,120]],'center_px':[100,70]}
        frame=np.zeros((150,210,3),np.uint8);r={'selected':c,'candidates':[c],'live_view':True,'outline_only':True}
        live=annotate(frame,r,legend=False);adopted=annotate(frame,{**r,'adopted_view':True},legend=False)
        self.assertTrue(np.any(np.all(live[20,30:170]==0,axis=1)))
        self.assertTrue(np.all(np.any(adopted[20,30:170]!=0,axis=1)))
        self.assertTrue(np.any(live[20,30:170]!=0))

class ConsensusUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_current_video_dual_outlines_limits_and_capture_metadata(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-consensus'
        cfg=json.loads((folder/'fixture.json').read_text());data=json.loads((folder/'observation.json').read_text());frame=cv2.imread(str(folder/'current.png'))
        a.profile=cfg['profile'];a.catalog.items=cfg['jigs'];a.catalog.revision+=1
        a.catalog.mesh=lambda key:{**cfg['jigs'][key]['mesh'],'shape':cfg['jigs'][key]['shape'],'method':cfg['jigs'][key]['method']}
        for r in data['by_jig'].values():r.update(pose_measured_at=time.monotonic(),pose_held=True)
        a.camera=SimpleNamespace(running=True,error=None,observation=(frame,data,time.monotonic()),close=lambda:None,join=lambda n:True)
        a.camera_sleeping=False;a.camera_view_requested=True
        a.camera_jig_choice.configure(values=['전체 지그',*[j['name'] for j in a.catalog.items.values()]])
        a.show_page('camera');a.camera_jig_choice.current(0);a.select_camera_jig();self.root.after_cancel(a.job)
        for widget,low,high in ((a.acquisition_seconds_spin,3,10),(a.measurement_attempts_spin,1,5),(a.hold_seconds_spin,1,10)):
            self.assertEqual(float(widget.cget('from')),low);self.assertEqual(float(widget.cget('to')),high)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update()
            for accepted in (False,True):
                a.camera.observation=(frame,data,time.monotonic());a.show_adoption.set(accepted);a.toggle_adoption_display();self.root.update();a.poll();self.root.after_cancel(a.job);a.draw_camera_frame(time.monotonic());self.root.update()
                self.assertLessEqual(a.hold_seconds_spin.winfo_rootx()+a.hold_seconds_spin.winfo_width(),self.root.winfo_rootx()+width)
                snapshot=a.camera_display_snapshot['result'];self.assertEqual(set(snapshot['by_jig']),set(cfg['jigs']))
                if accepted:self.assertTrue(all(r['selected']['consensus_median'] for r in snapshot['adopted_by_jig'].values()))
                x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/jig-consensus-{width}x{height}-{accepted}.png')
        # With adopted display enabled, the live outline is still rendered as a
        # separate dashed layer rather than replaced by the accepted result.
        original=deepcopy(data['by_jig']);live=deepcopy(data['live_by_jig']['pallet']['selected'])
        for key in ('quad','outline_px','axes_px'):
            if key in live:live[key]=(np.asarray(live[key])+[70,0]).tolist()
        live['center_px']=(np.asarray(live['center_px'])+[70,0]).tolist()
        data['live_by_jig']['pallet'].update(selected=live,candidates=[live]);a.camera.observation=(frame,data,time.monotonic());a.draw_camera_frame(time.monotonic())
        self.assertEqual(data['by_jig'],original)
        self.assertEqual(a.camera_display_snapshot['result']['by_jig']['pallet']['selected']['center_px'],live['center_px'])
