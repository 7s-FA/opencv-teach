import unittest,time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from PIL import ImageGrab
from tests import test_ui as fixtures
from so101_teach.geometry import _initial_corrected_step
from so101_teach.jig_comparison import comparison_groups

class TeachingSessionBasisTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    fake_jig_camera=fixtures.UITests.fake_jig_camera
    def two_sessions(self):
        a=self.app;self.fake_jig_camera((228,-138,67));a.toggle_jig_updates();a.follow_jig.set(True);a.commit_target()
        first=deepcopy(a.episode['steps'][0]);a.toggle_jig_updates();self.fake_jig_camera((260,-120,74));a.toggle_jig_updates()
        return a,first
    def test_repause_saves_new_basis_without_changing_old_step_or_ticks(self):
        a,first=self.two_sessions();ticks=deepcopy(a.target);a.commit_target();second=a.episode['steps'][-1]
        self.assertEqual(a.episode['steps'][0],first);self.assertEqual(second['ticks'],ticks)
        self.assertEqual(second['jig_reference']['pose'],[260,-120,74]);self.assertEqual(a.episode['jig_references']['pallet'],first['jig_reference'])
        loaded=a.store.load(a.save_episode());self.assertEqual(loaded['steps'],a.episode['steps'])
        groups=comparison_groups(loaded['steps'],a.catalog.items,{'pallet':second['jig_reference']})
        self.assertEqual(len(groups),2);self.assertNotEqual(groups[0]['delta'],[0,0,0]);self.assertEqual(groups[1]['delta'],[0,0,0])
        # A newly taught step at the current jig pose must receive no second
        # offset when it is resolved for execution.
        plan=_initial_corrected_step(a.kin,second,second['jig_reference'],a.catalog.mesh('pallet')['sha256'])
        self.assertEqual(plan['ticks'],ticks);self.assertEqual(plan['position_error_mm'],0.)
    def test_old_step_edit_and_move_keep_its_original_basis(self):
        a,first=self.two_sessions();a.step_name.set('이름 수정');a.update_selected_step()
        self.assertEqual(a.episode['steps'][0]['jig_reference'],first['jig_reference'])
        with patch.object(a,'prepare_execution') as execute:
            a.execute_target();self.assertEqual(execute.call_args.args[1][0]['jig_reference'],first['jig_reference'])
    def test_missing_new_paused_pose_does_not_fall_back_to_episode_basis(self):
        a,first=self.two_sessions();a.invalidate_jig_measurements();before=deepcopy(a.episode)
        with self.assertRaisesRegex(ValueError,'지그 다시 읽기'):a.commit_target()
        self.assertEqual(a.episode,before)
    def test_repause_without_a_new_measurement_requests_read_instead_of_old_hold(self):
        a=self.app;self.fake_jig_camera();a.toggle_jig_updates();a.follow_jig.set(True);a.commit_target()
        a.toggle_jig_updates()
        with patch.object(a,'request_jig_read') as read:a.toggle_jig_updates()
        read.assert_called_once();self.assertIsNone(a.new_step_jig_reference('pallet'))
        with self.assertRaises(ValueError):a.commit_target()
    def test_resume_and_reload_continue_latest_saved_basis_until_next_pause(self):
        a,first=self.two_sessions();a.commit_target();second=deepcopy(a.episode['steps'][-1]);a.toggle_jig_updates()
        self.fake_jig_camera((280,-100,80));a.step_name.set('추가');a.commit_target()
        self.assertEqual(a.episode['steps'][-1]['jig_reference'],second['jig_reference'])
        a.episode=a.store.load(a.save_episode());self.assertEqual(a.new_step_jig_reference('pallet'),second['jig_reference'])
    def test_second_editor_adds_new_basis_but_keeps_old_basis_on_update(self):
        a,first=self.two_sessions();self.root.update();box=a.steps.bbox(first['id']);a.open_second_editor(SimpleNamespace(y=box[1]+8));b=a.second_editor
        self.assertIsNotNone(b);b.step_name.set('편집 2 수정');b.update_selected_step()
        self.assertEqual(a.episode['steps'][0]['jig_reference'],first['jig_reference'])
        b.step_name.set('편집 2 추가');b.commit_target();self.assertEqual(a.episode['steps'][-1]['jig_reference']['pose'],[260,-120,74])
    def test_old_and_new_basis_hint_fits_background_window(self):
        a,first=self.two_sessions();a.update_jig_hint();self.assertIn('새 스텝 기준',a.jig_hint.get())
        for width,height in ((1280,800),(1180,760)):
            self.root.geometry(f'{width}x{height}');self.root.update();x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+width,y+height)).save(f'/tmp/teaching-session-{width}x{height}.png')
