import json,time,unittest
from copy import deepcopy
from pathlib import Path
from PIL import ImageGrab
from tests import test_ui as fixtures
from so101_teach.domain import ROOT

class CurrentCarrierPreviewTests(unittest.TestCase):
    def setUp(self):
        import tempfile,shutil,tkinter as tk
        from so101_teach.ui import App
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        for name in ('profile.json','jigs.json','model-adjustments.json'):
            if (ROOT/'data'/name).exists():shutil.copy2(ROOT/'data'/name,self.data/name)
        shutil.copytree(ROOT/'data/calibration',self.data/'calibration')
        self.root=tk.Tk();self.app=App(self.root,self.data,render=False,auto_camera=False);self.root.geometry('1280x800');self.root.update()
    tearDown=fixtures.UITests.tearDown
    def test_existing_episode_keeps_saved_pose_but_displays_current_assembly(self):
        a=self.app;key='b5ebdb54807c441c8267c15d77713b6a'
        episode=json.loads((Path(__file__).parent/'fixtures/carrier-episode.json').read_text());before=deepcopy(episode)
        a.catalog.items=json.loads((ROOT/'data/jigs.json').read_text());a.catalog.revision+=1
        a.step_jig_choice.configure(values=[d['name'] for d in a.catalog.items.values()])
        a.episode=episode;a.episode_name.set(episode['name']);step=next(s for s in episode['steps'] if s.get('jig_id')==key)
        a.selected=step['id'];a.follow_jig.set(True);a.set_step_jig(key);a.apply_target(step['ticks']);a.refresh_steps();a.update_jig_hint()
        a.render_enabled=True;a.view=(90.,-65.,.50,step['jig_reference']['pose'][0]/1000,step['jig_reference']['pose'][1]/1000,.025)
        deadline=time.monotonic()+15
        while a.last_rgb is None and time.monotonic()<deadline:self.root.update();time.sleep(.03)
        self.assertIsNotNone(a.last_rgb,a.message.get())
        rendered=next(j for j in a.last_render[2]['jigs'] if j['id']==key)
        self.assertEqual(rendered['pose'],step['jig_reference']['pose']);self.assertEqual(rendered['mesh_yaw_offset_deg'],180.)
        self.assertEqual(rendered['stl'],a.catalog.mesh(key)['stl_path']);self.assertAlmostEqual(rendered['size_mm'][2],31.3,places=3)
        self.assertEqual(step['jig_reference']['stl_sha256'],before['jig_references'][key]['stl_sha256'])
        self.assertEqual(step['jig_reference']['symmetry_deg'],360)
        self.assertIn('운반용 지그: 현재 모델',a.preview_caption.get());self.assertEqual(a.episode,before)
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update()
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/current-carrier-saved-step-{width}x{height}.png')
