"""Paired PC recordings preserve time, source pixels and inspection identity."""
from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy
import time
import unittest
from unittest.mock import Mock,patch
import cv2
import numpy as np
from PIL import Image
from desktop_monitor.recording import PreviewRecording
from desktop_monitor.recording_visual import render_inspection
from tests import test_work_monitor_recording as ui_fixture


def scene():
    return {'image':Image.new('RGB',(1280,720),'red'),'source_size':(1280,720),'outlines':[],
        'checks':[{'title':'A 팔레트 · 통과','region':[[160,90],[1120,90],[1120,630],[160,630]],
                   'contours':[[[400,250],[800,250],[800,500],[400,500]]],'tone':'pass'}],
        'heading':'판정 영상 · 1초 전 · 15:00:00 촬영','detail':'A 하단 · 정상 · 통과 · 안착 영역 안'}


class PairedRecordingTests(unittest.TestCase):
    def test_both_movies_share_timeline_but_keep_original_and_inspected_pixels_separate(self):
        with TemporaryDirectory() as folder:
            recorder=PreviewRecording(folder,now=100,fps=5);raw=Image.new('RGB',(320,180),'blue');view=scene()
            for at in (100,100.4,101):recorder.offer(raw,at,inspection=lambda now:view)
            # Queued frames and geometry are immutable snapshots.
            raw.paste('green',(0,0,320,180));view['image'].paste('green',(0,0,1280,720));view['checks'].clear()
            recorder.finish();recorder.start();recorder.join(3)
            self.assertTrue(recorder.done.is_set());self.assertIsNone(recorder.error)
            self.assertEqual(recorder.frames,6);self.assertFalse(recorder.partial.exists());self.assertFalse(recorder.inspection_partial.exists())
            self.assertTrue(recorder.path.name.endswith('_original.mp4'));self.assertTrue(recorder.inspection_path.name.endswith('_inspection.mp4'))
            frames=[]
            for path,height in ((recorder.path,180),(recorder.inspection_path,180)):
                capture=cv2.VideoCapture(str(path))
                try:
                    self.assertEqual(capture.get(cv2.CAP_PROP_FPS),5);self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_COUNT),6)
                    self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_HEIGHT),height)
                    decoded=[]
                    while True:
                        ok,frame=capture.read()
                        if not ok:break
                        decoded.append(frame)
                    self.assertEqual(len(decoded),6);frames.append(decoded[0])
                finally:capture.release()
            self.assertGreater(frames[0][80,160,0],200);self.assertLess(frames[0][80,160,2],20)
            self.assertGreater(frames[1][80,160,2],200);self.assertLess(frames[1][80,160,0],20)
            self.assertEqual(frames[0].shape,frames[1].shape)  # No recording-only footer.
    def test_annotation_renderer_has_no_extra_footer_and_keeps_source_unchanged(self):
        view=scene();original=np.asarray(view['image']).copy()
        image=render_inspection(Image.new('RGB',(320,180),'blue'),(320,180),view)
        self.assertEqual(image.size,(320,180));np.testing.assert_array_equal(np.asarray(view['image']),original)
        self.assertEqual(image.getpixel((160,5)),(255,0,0))
        self.assertEqual(image.getpixel((160,179)),(255,0,0))
    def test_second_encoder_failure_releases_both_and_never_claims_a_complete_pair(self):
        with TemporaryDirectory() as folder,patch('cv2.VideoWriter') as factory:
            first=Mock();first.isOpened.return_value=True;second=Mock();second.isOpened.return_value=False
            factory.side_effect=[first,second]
            recorder=PreviewRecording(folder,now=0);recorder.offer(Image.new('RGB',(64,48)),0)
            recorder.finish();recorder.start();recorder.join(3)
            self.assertTrue(recorder.done.is_set());self.assertIn('인코더',recorder.error)
            first.release.assert_called_once();second.release.assert_called_once()
            self.assertFalse(recorder.path.exists());self.assertFalse(recorder.inspection_path.exists())
    def test_pair_finalization_failure_does_not_leave_one_file_marked_complete(self):
        with TemporaryDirectory() as folder:
            recorder=PreviewRecording(folder,now=0);recorder.offer(Image.new('RGB',(320,180)),0);recorder.finish()
            original=Path.replace
            def replace(path,target):
                if path==recorder.inspection_partial:raise OSError('disk unavailable')
                return original(path,target)
            with patch.object(Path,'replace',replace):recorder.start();recorder.join(3)
            self.assertTrue(recorder.done.is_set());self.assertIn('disk unavailable',recorder.error)
            self.assertFalse(recorder.path.exists());self.assertFalse(recorder.inspection_path.exists())
            self.assertTrue(recorder.partial.exists());self.assertTrue(recorder.inspection_partial.exists())


class PairedRecordingUITests(unittest.TestCase):
    setUp=ui_fixture.MonitorRetentionRecordingUITests.setUp
    tearDown=ui_fixture.MonitorRetentionRecordingUITests.tearDown
    panel=ui_fixture.MonitorRetentionRecordingUITests.panel
    passed=ui_fixture.MonitorRetentionRecordingUITests.passed

    def test_inspection_copy_uses_the_displayed_frame_and_annotations(self):
        p=self.panel();sample=self.passed();sample['frame']=Image.new('RGB',(1280,720),'blue')
        sample['inspection_frame']=Image.new('RGB',(960,540),'red');p.consume(sample)
        view=p.recording_inspection(time.monotonic())
        self.assertEqual(view['image'].getpixel((1,1)),(255,0,0));self.assertTrue(view['checks'][0]['contours'])
        self.assertIn('판정 영상',view['heading']);self.assertIn('통과',view['detail']);self.assertIn('촬영',view['heading'])
        self.assertEqual(p.frame.getpixel((1,1)),(0,0,255))
    def test_new_job_cannot_borrow_old_inspection_video(self):
        p=self.panel();sample=self.passed();p.consume(sample);p.toggle_evidence()
        new=deepcopy(sample);new['runs'][0]['job_id']='new';new['visual']={};new['inspection_frame']=None
        new['frame']=Image.new('RGB',(1280,720),'blue');p.consume(new)
        view=p.recording_inspection(time.monotonic())
        self.assertIn('직전 검사 결과',view['heading']);self.assertTrue(view['checks'])
        p.snapshot['frame_received']=p.hold_until+.1
        with patch('desktop_monitor.ui.time.monotonic',return_value=p.hold_until+.1):p.paint()
        view=p.recording_inspection(p.hold_until+.1)
        self.assertEqual(view['image'].getpixel((1,1)),(0,0,255));self.assertEqual(view['checks'],[])
        self.assertTrue(view['heading'].startswith('실시간'))

    def test_late_pass_does_not_label_an_incompatible_observation_as_passed(self):
        p=self.panel();sample=self.passed()
        sample['visual']['checks']['step']['check']['expected']='cap_added'
        p.consume(sample);view=p.recording_inspection(time.monotonic())
        self.assertNotEqual(view['checks'][0]['tone'],'pass')
        self.assertNotIn('통과',view['detail']);self.assertIn('판정 프레임 확인 중',view['detail'])
