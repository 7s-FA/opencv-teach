import json,shutil,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from PIL import ImageGrab
from so101_teach.domain import ROOT
from so101_teach.ui import App
from so101_teach.remote_config import configuration_bundle

class WorkcellPreviewUITests(unittest.TestCase):
    def test_workcell_button_renders_two_arms_without_device_connection_or_remote_config_change(self):
        out=ROOT/'verification/workcell-board-tcp'
        with tempfile.TemporaryDirectory() as folder:
            data=Path(folder)
            for name in ('profile.json','jigs.json','workcell-preview.json'):
                shutil.copy2(ROOT/'data'/name,data/name)
            shutil.copytree(ROOT/'data/calibration',data/'calibration')
            root=tk.Tk();app=None
            with patch('so101_teach.ui.MotionSession.start',side_effect=AssertionError('No motor connection')):
                try:
                    app=App(root,data,render=True,auto_camera=False,auto_devices=False);root.geometry('1280x800');root.update()
                    profile_before=json.loads((data/'profile.json').read_text());bundle_before=configuration_bundle(app)
                    state=json.loads((out/'state.json').read_text());app.apply_target(state['follower']['latest']['ticks'])
                    def walk(w):
                        yield w
                        for child in w.winfo_children():yield from walk(child)
                    button=next(w for w in walk(root) if w.winfo_class()=='TButton' and str(w.cget('text'))=='전체 작업대')
                    button.invoke()
                    deadline=time.monotonic()+20
                    while app.last_rgb is None and time.monotonic()<deadline:root.update();time.sleep(.03)
                    self.assertIsNotNone(app.last_rgb,app.message.get());self.assertIn('로봇팔3',app.preview_caption.get())
                    self.assertEqual(app.last_render[2]['workcell']['robot_id'],'arm3')
                    self.assertIn('배치 확정',app.preview_caption.get())
                    self.assertFalse(app.workcell_preview['verified_for_robot_motion'])
                    self.assertIn('팔2 TCP',app.tcp_label.get());self.assertIn('팔3 TCP(예시)',app.tcp_label.get())
                    self.assertIn('board',app.last_render[2]['workcell']);self.assertIn('linear_stage',app.last_render[2]['workcell'])
                    board=app.last_render[2]['workcell']['board'];self.assertEqual(board['enclosure']['sides'],4);self.assertEqual(board['backdrop']['height_mm'],400)
                    carrier=next(j for j in app.last_render[2]['jigs'] if j.get('platform')=='TurtleBot3 Burger')
                    self.assertEqual(carrier['platform_height_mm'],195);self.assertAlmostEqual(carrier['bottom_z_mm'],app.profile['table_z_mm'])
                    self.assertEqual(configuration_bundle(app),bundle_before)
                    self.assertEqual(json.loads((data/'profile.json').read_text()),profile_before)
                    self.assertIsNone(app.session)
                    for width,height in ((1280,800),(1180,760)):
                        root.geometry(f'{width}x{height}');root.update()
                        self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),root.winfo_rootx()+width)
                        self.assertLessEqual(app.capture_btn.winfo_rooty()+app.capture_btn.winfo_height(),root.winfo_rooty()+height)
                        x,y=root.winfo_rootx(),root.winfo_rooty();ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(out/f'workcell-ui-{width}x{height}.png')
                finally:
                    if app:app.close()
                    else:root.destroy()
