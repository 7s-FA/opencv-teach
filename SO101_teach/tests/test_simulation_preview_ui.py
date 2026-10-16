import json,time,unittest
from unittest.mock import patch
import numpy as np
from PIL import ImageGrab
from so101_teach.domain import ROOT
from tests import test_arm_workspaces as fixtures

class SimulationPreviewUITests(unittest.TestCase):
    setUp=fixtures.ArmWorkspacesTests.setUp
    tearDown=fixtures.ArmWorkspacesTests.tearDown
    def test_rendered_both_arms_match_corrected_angles_and_tcp_without_device_access(self):
        out=ROOT/'verification/restore_original_ik_20261013';out.mkdir(parents=True,exist_ok=True)
        for arm,eid,index in [('arm2','dea969d731e8449494285c4fbc8ca066',15),('arm3','8851f8f6f68343588131edf685bfbf30',4)]:
            app=self.manager.apps[arm];self.manager.select(arm);app.root.geometry('1920x1080+0+0');app.root.update()
            ep=json.loads((ROOT.parent/'SO101_rl/config/reference_episodes'/f'{eid}.json').read_text())
            app.episode=ep;app.apply_target(ep['steps'][index]['ticks']);app.refresh_steps();app.set_mode('target');app.render_enabled=True
            app.last_render=None;app.last_rgb=None
            with patch.object(app,'connect_devices') as connect,patch.object(app,'motion_request') as move:
                deadline=time.monotonic()+25
                while (app.last_rgb is None or app.pending_render_context is not None) and time.monotonic()<deadline:
                    app.root.update();time.sleep(.03)
                self.assertIsNotNone(app.last_rgb)
                np.testing.assert_allclose(app.last_render[0],app.preview_reference.angles(app.target),atol=1e-12)
                self.assertIn('시뮬 각도 보정 적용 · 실행은 기존 식',app.preview_caption.get())
                self.assertIn('시뮬 보정 적용',app.preview_title.get())
                self.assertIs(app.kin.reference,app.reference)
                np.testing.assert_allclose(app.kin.fk(app.target),app.kin.fk_angles(app.reference.angles(app.target)),atol=1e-12)
                self.assertIsNot(app.preview_reference,app.reference)
                self.assertIsNone(app.session);connect.assert_not_called();move.assert_not_called()
                ImageGrab.grab().save(out/f'{arm}_app_preview.png')
