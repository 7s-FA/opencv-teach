import json,time,unittest
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
from PIL import ImageGrab
import cv2
from . import test_ui as fixtures
from so101_teach.domain import ROOT
from so101_teach.vision_service import MultiDetector

class DirectionUITests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_pause_keeps_full_heading_and_simulation_uses_the_same_pose(self):
        a=self.app;folder=Path(__file__).parent/'fixtures/vision-assembly';cfg=json.loads((folder/'fixture.json').read_text());f=cv2.imread(str(folder/'current.png'));key='b5ebdb54807c441c8267c15d77713b6a'
        a.profile=cfg['profile'];a.catalog.items=deepcopy(cfg['jigs']);a.catalog.revision+=1
        for j in a.catalog.items.values():j['stl']=str(ROOT/'data/jig_assets'/Path(j['stl']).name)
        a.catalog.items[key]['stl']=str(ROOT/'assets/carrier_assembly/carrier_assembly.stl')
        data=MultiDetector(a.catalog,a.profile).process(f);data['by_jig']=deepcopy(data['live_by_jig']);now=time.monotonic()
        for r in data['by_jig'].values():r.update(pose_measured_at=now-.2,pose_held=True,hold_remaining_s=9.8)
        a.camera=SimpleNamespace(running=True,error=None,observation=(f,data,now-1.2),close=lambda:None,join=lambda n:True)
        a.camera_sleeping=False;a.toggle_jig_updates();self.assertIsNone(a.camera_task);self.assertEqual(len(a.teaching_jig_results),2)
        reference=a.current_jig_reference(key);self.assertEqual(reference['symmetry_deg'],360);self.assertLess(abs(reference['pose'][2]-29.4),2);self.assertEqual(reference['mesh_yaw_offset_deg'],180)
        a.render_enabled=True;a.view=(90.,-65.,.48,reference['pose'][0]/1000,reference['pose'][1]/1000,.025);a.show_page('model')
        deadline=time.monotonic()+15
        while a.last_rgb is None and time.monotonic()<deadline:self.root.update();time.sleep(.03)
        self.assertIsNotNone(a.last_rgb,(a.message.get(),a.mode,a.render_context,a.displayed_token, (a.renderer.process.is_alive(),a.renderer.process.exitcode,a.renderer.in_flight,a.renderer.token) if a.renderer else None))
        rendered=next(j for j in a.last_render[2]['jigs'] if j['id']==key);self.assertEqual(rendered['pose'],reference['pose']);self.assertEqual(rendered['mesh_yaw_offset_deg'],180)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update()
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/heading-basis-simulation-{width}x{height}.png')
