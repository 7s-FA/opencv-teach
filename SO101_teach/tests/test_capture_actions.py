"""Mouse capture/delete actions and stable capture-button feedback."""
import time
from pathlib import Path
from unittest.mock import patch
import unittest
from tests.test_capture_gallery import CaptureGalleryTests as Fixture


class CaptureActionTests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    prepare=Fixture.prepare

    def test_capture_feedback_does_not_change_button_text_or_state(self):
        p=self.prepare();a=self.app
        p.update_frame(time.monotonic());label=p.save_button.cget('text')
        with patch.object(p.save_button,'configure',wraps=p.save_button.configure) as configure,patch.object(p.save_button,'state',wraps=p.save_button.state) as state:
            p.save_button.invoke()
            for offset in (0.05,0.21,0.30):p.update_frame(time.monotonic()+offset)
        self.assertEqual(p.save_button.cget('text'),label)
        self.assertEqual(configure.call_count,0)
        self.assertEqual(state.call_count,0)
        self.assertEqual(len(p.entries),1)

    def test_right_click_deletes_selected_bundle_offline_and_keeps_other_photo(self):
        p=self.prepare();a=self.app;first=a.save_camera()
        p.capture_ready_at=0
        frame,data,_=a.camera.observation;a.camera.observation=(frame.copy(),data,time.monotonic())
        second=a.save_camera();a.camera=None
        p.table.selection_set(first.name);p.select_photo();p.update_frame(time.monotonic())
        self.assertFalse(p.save_button.instate(['disabled']))
        p.save_button.event_generate('<Button-3>');self.root.update()
        for kind,suffix in [('원본','.png'),('화면 표시본','.png'),('판정 기록','.json')]:
            self.assertFalse((p.folder/kind/Path(first.name).with_suffix(suffix)).exists())
            self.assertTrue((p.folder/kind/Path(second.name).with_suffix(suffix)).exists())
        self.assertEqual(p.selected,second.name)
        # A right-button double click must not spill into the next photo.
        p.save_button.event_generate('<Button-3>');self.root.update()
        self.assertIn(second.name,p.entries)

    def test_delete_handles_missing_variant_and_empty_selection(self):
        p=self.prepare();a=self.app;path=a.save_camera();path.unlink()
        p.delete_selected()
        self.assertFalse(p.entries);self.assertIsNone(p.saved_image)
        self.assertEqual(list(p.folder.rglob('*.png')),[])
        self.assertEqual(list(p.folder.rglob('*.json')),[])
        p.delete_ready_at=0
        with self.assertRaisesRegex(ValueError,'선택'):p.delete_selected()

    def test_delete_rejects_paths_outside_capture_subfolders(self):
        p=self.prepare();a=self.app;path=a.save_camera()
        outside=self.data/'unrelated';outside.mkdir();sentinel=outside/path.name;sentinel.write_bytes(b'keep')
        original=p.folder/'원본';(original/path.name).unlink();original.rmdir();original.symlink_to(outside,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'폴더'):p.delete_selected()
        self.assertEqual(sentinel.read_bytes(),b'keep');self.assertTrue(path.exists())
