import unittest
import numpy as np
import mujoco
from so101_teach.preview import configured_scene,reference_points
from so101_teach.configuration import model_tcp

class BaseMarkerTests(unittest.TestCase):
    def test_markers_follow_model_frames_without_changing_geometry(self):
        m=mujoco.MjModel.from_xml_string(configured_scene([],model_tcp()));d=mujoco.MjData(m);mujoco.mj_forward(m,d)
        first=reference_points(m,d);np.testing.assert_allclose(first['base'],[0,0,0],atol=1e-12)
        np.testing.assert_allclose(d.site('base_origin').xpos,first['base'])
        for name,angle in [('shoulder_pan',.4),('shoulder_lift',.7),('elbow_flex',-.3)]:d.qpos[m.joint(name).qposadr[0]]=angle
        m.geom('preview_floor').pos[2]=-.03;mujoco.mj_forward(m,d);later=reference_points(m,d)
        np.testing.assert_array_equal(first['base'],later['base']);self.assertGreater(np.linalg.norm(first['tcp']-later['tcp']),.01)
        frame=d.body('gripper_frame_link');expected=frame.xpos+frame.xmat.reshape(3,3)@np.array(model_tcp()['xyz_mm'])/1000
        np.testing.assert_allclose(later['tcp'],expected,atol=1e-12)

class ReferenceGuideUITests(unittest.TestCase):
    from tests import test_ui as fixtures
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def test_guide_is_right_of_coordinates_when_space_allows(self):
        a=self.app;self.root.geometry('1480x920');self.root.update()
        self.assertEqual(a.preview_guide.grid_info()['column'],1)
        self.assertGreater(a.preview_guide.winfo_rootx(),a.preview_tcp_label.winfo_rootx()+a.preview_tcp_label.winfo_width())
        self.root.geometry('1180x760');self.root.update()
        self.assertLessEqual(a.preview_guide.winfo_rootx()+a.preview_guide.winfo_width(),a.preview_card.winfo_rootx()+a.preview_card.winfo_width())
    def test_help_describes_current_controls_and_has_no_playback_speed_instructions(self):
        from so101_teach.help_content import HELP_SECTIONS
        text='\n'.join(body for _,body in HELP_SECTIONS)
        for phrase in ('편집 2','자동 갱신 정지','300 / 보통 350 / 빠르게 400','베이스 원점','배속 선택은 없습니다'):self.assertIn(phrase,text)
        self.assertNotIn('0.5/1/2',text);self.app.open_help()
        self.assertEqual(self.app.settings.tabs.select(),str(self.app.settings.pages['help']))
