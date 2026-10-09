from types import SimpleNamespace
import time,unittest
from tests import test_ui
from so101_teach.domain import JOINTS,ROOT,Snapshot

class DualMonitorTests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown
    def test_two_arms_are_independent_and_stale_values_clear(self):
        a=self.app;now=time.monotonic()
        def sample(tick,age=0):
            return Snapshot('follower',dict.fromkeys(JOINTS,tick),{k:{'goal_ticks':tick+10,'torque':1,'voltage_v':12.1,'temperature_c':32,'load_raw':1536,'current_raw':18,'status':0} for k in JOINTS},now-age,time.time(),a.calibration.sha256,True,'test')
        first=SimpleNamespace(running=True,state='HOLD',latest=sample(1900),error=None)
        second=SimpleNamespace(running=True,state='MOVING',latest=sample(2300),error=None)
        other=SimpleNamespace(session=second,latest=None,remote=None,calibration=a.calibration)
        a.session=first;a.workspace_manager=SimpleNamespace(apps={'arm2':a,'arm3':other})
        try:
            if a.job:self.root.after_cancel(a.job);a.job=None
            if a.render_job:self.root.after_cancel(a.render_job);a.render_job=None
            a.show_page('devices');self.root.geometry('1920x1080');self.root.update();a.dual_monitor.update(now,force=True)
            cards=a.dual_monitor.cards
            self.assertTrue(cards['arm2']['tree'].item(JOINTS[0],'values')[1].startswith('1900 ('))
            self.assertTrue(cards['arm3']['tree'].item(JOINTS[0],'values')[1].startswith('2300 ('))
            self.assertEqual(cards['arm3']['tree'].item(JOINTS[0],'values')[6],'1536 (50.0%)')
            self.assertIn('이동 중',cards['arm3']['title'].get())
            from PIL import ImageGrab
            self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            self.assertLessEqual(a.hold_btn.winfo_rooty()+a.hold_btn.winfo_height(),y+self.root.winfo_height())
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save(ROOT/'verification/episode-adjust-sync/two-arms-maximized.png')
            second.latest=sample(2300,2);a.dual_monitor.update(now,force=True)
            self.assertEqual(cards['arm3']['tree'].item(JOINTS[0],'values')[1],'—')
            self.assertTrue(cards['arm2']['tree'].item(JOINTS[0],'values')[1].startswith('1900 ('))
        finally:a.session=None;a.workspace_manager=None
