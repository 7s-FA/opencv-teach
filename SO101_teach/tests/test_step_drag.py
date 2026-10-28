"""Native step selection and deliberate, indicated reordering."""
import unittest
from types import SimpleNamespace
from tests.test_ui import UITests as Fixture

class StepDragTests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown

    def prepare(self):
        a=self.app
        for name in ('A','B','C'):a.step_name.set(name);a.commit_target()
        self.root.update()
        return [s['id'] for s in a.episode['steps']]

    def point(self,key,fraction=.5):
        x,y,w,h=self.app.steps.bbox(key)
        return SimpleNamespace(x=x+20,y=int(y+h*fraction))

    def names(self):return [s['name'] for s in self.app.episode['steps']]

    def test_press_immediately_highlights_source_and_small_boundary_motion_does_not_reorder(self):
        a=self.app;ids=self.prepare();start=self.point(ids[0],.95);end=self.point(ids[1],.02)
        a.left_step_press(start)
        self.assertEqual(a.steps.selection(),(ids[0],));self.assertEqual(a.selected,ids[0])
        a.drag_step_motion(end);a.drop_step(end)
        self.assertEqual(self.names(),['A','B','C']);self.assertIsNone(a.drag_step)

    def test_release_at_other_row_without_drag_does_not_reorder(self):
        a=self.app;ids=self.prepare();a.left_step_press(self.point(ids[0]));a.drop_step(self.point(ids[2]))
        self.assertEqual(self.names(),['A','B','C'])

    def test_drag_keeps_source_selected_shows_marker_and_saves_once(self):
        a=self.app;ids=self.prepare();start=self.point(ids[0]);end=self.point(ids[2],.8)
        a.steps.event_generate('<ButtonPress-1>',x=start.x,y=start.y);self.root.update()
        a.steps.event_generate('<B1-Motion>',x=end.x,y=end.y);self.root.update()
        self.assertEqual(a.steps.selection(),(ids[0],));self.assertTrue(a.step_drop_marker.winfo_ismapped())
        self.assertEqual(str(a.steps.cget('cursor')),'fleur')
        a.steps.event_generate('<ButtonRelease-1>',x=end.x,y=end.y);self.root.update()
        self.assertEqual(self.names(),['B','C','A']);self.assertFalse(a.step_drop_marker.winfo_ismapped())
        saved=a.store.load(a.store.directory/(a.episode['id']+'.json'))
        self.assertEqual([s['name'] for s in saved['steps']],self.names())
        a.drop_step(start);self.assertEqual(self.names(),['B','C','A'])

    def test_drop_before_row_preserves_expected_order(self):
        a=self.app;ids=self.prepare();end=self.point(ids[1],.1)
        a.left_step_press(self.point(ids[2]));a.drag_step_motion(end);a.drop_step(end)
        self.assertEqual(self.names(),['A','C','B'])

    def test_escape_and_outside_drop_cancel(self):
        a=self.app;ids=self.prepare();end=self.point(ids[2],.8)
        a.left_step_press(self.point(ids[0]));a.drag_step_motion(end);a.stop_all();a.drop_step(end)
        self.assertEqual(self.names(),['A','B','C'])
        a.left_step_press(self.point(ids[0]));a.drag_step_motion(end)
        a.drop_step(SimpleNamespace(x=-10,y=end.y));self.assertEqual(self.names(),['A','B','C'])

    def test_safe_boundaries_and_adjustment_tab_cannot_be_reordered(self):
        a=self.app;ids=self.prepare();a.episode['steps'][0]['safe_boundary']='start';a.episode['steps'][-1]['safe_boundary']='end';a.refresh_steps();self.root.update()
        a.left_step_press(self.point(ids[0]));self.assertIsNone(a.drag_step)
        end=self.point(ids[2]);a.left_step_press(self.point(ids[1]));a.drag_step_motion(end);a.drop_step(end)
        self.assertEqual(self.names(),['A','B','C'])
        a.open_episode_adjust();self.root.update();a.left_step_press(self.point(ids[1]));self.assertIsNone(a.drag_step)
