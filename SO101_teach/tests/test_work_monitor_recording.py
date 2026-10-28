"""Desktop-only evidence retention and encoding; no robot/Pi connection."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock,patch
import cv2
from PIL import Image
from desktop_monitor.recording import PreviewRecording
from tests import test_work_visuals as visuals


class PreviewRecordingTests(unittest.TestCase):
    def test_real_mp4_has_elapsed_duration_fixed_size_and_live_colors(self):
        for fps in (2,5,10):
            with TemporaryDirectory() as folder:
                recorder=PreviewRecording(folder,fps=fps,now=100)
                # Deliberate timer jitter: fill the missed 1.0 s slot with its preceding frame.
                for at,color,size in [(100,'red',(1280,720)),(100+1.04/fps,'green',(1280,720)),(100+3.2/fps,'blue',(640,480))]:
                    recorder.offer(Image.new('RGB',size,color),at)
                recorder.finish();recorder.start();recorder.join(3)
                self.assertTrue(recorder.done.is_set());self.assertIsNone(recorder.error)
                self.assertFalse(recorder.partial.exists());self.assertTrue(recorder.path.is_file())
                video=cv2.VideoCapture(str(recorder.path))
                try:
                    self.assertTrue(video.isOpened());self.assertEqual(video.get(cv2.CAP_PROP_FPS),fps)
                    self.assertEqual(video.get(cv2.CAP_PROP_FRAME_COUNT),4)
                    self.assertEqual((video.get(cv2.CAP_PROP_FRAME_WIDTH),video.get(cv2.CAP_PROP_FRAME_HEIGHT)),(960,540))
                    frames=[]
                    while True:
                        ok,frame=video.read()
                        if not ok:break
                        frames.append(frame)
                    self.assertEqual(len(frames),4)
                    self.assertGreater(frames[0][200,200,2],200)
                    self.assertGreater(frames[-1][200,200,0],200)
                finally:video.release()
    def test_backpressure_keeps_latest_frames_without_ending_recording(self):
        with TemporaryDirectory() as folder:
            recorder=PreviewRecording(folder,fps=2,now=0)
            for index in range(8):recorder.offer(Image.new('RGB',(64,48),'white'),index*.5)
            self.assertFalse(recorder.stopping.is_set());self.assertEqual(recorder.jobs.qsize(),4)
            self.assertEqual(recorder.coalesced_frames,4)
            recorder.finish();recorder.start();recorder.join(3)
            self.assertTrue(recorder.done.is_set());self.assertIsNone(recorder.error)
            self.assertTrue(recorder.path.exists());self.assertEqual(recorder.frames,8)
    def test_encoder_failure_finishes_without_success_file(self):
        with TemporaryDirectory() as folder,patch('cv2.VideoWriter') as writer:
            writer.return_value.isOpened.return_value=False
            recorder=PreviewRecording(folder,fps=2,now=0);recorder.offer(Image.new('RGB',(64,48)),0)
            recorder.start();recorder.finish();recorder.join(3)
            self.assertTrue(recorder.done.is_set());self.assertIn('인코더',recorder.error)
            self.assertFalse(recorder.path.exists());writer.return_value.release.assert_called_once()
    def test_empty_recording_cannot_create_completed_movie(self):
        with TemporaryDirectory() as folder:
            recorder=PreviewRecording(folder,fps=2,now=0)
            recorder.finish();recorder.start();recorder.join(3)
            self.assertIn('저장할 영상 프레임',recorder.error);self.assertTrue(recorder.done.is_set())
            self.assertFalse(recorder.path.exists())


class MonitorRetentionRecordingUITests(unittest.TestCase):
    setUp=visuals.VisualUITests.setUp
    tearDown=visuals.VisualUITests.tearDown
    panel=visuals.VisualUITests.panel

    def passed(self):
        sample=visuals.visual_sample();run=sample['runs'][0]
        sample['events'].append({'at':time.time(),'arm':'arm2','run_id':run['run_id'],'sequence':5,'status':'A_INSPECTION_PASS:step'})
        return sample

    def test_result_survives_clear_and_motion_then_returns_to_live_after_two_seconds(self):
        p=self.panel();sample=self.passed();now=sample['received']
        with patch('desktop_monitor.ui.time.monotonic',return_value=now):
            p.consume(sample);self.assertIn('2초 유지',p.camera_status.get())
        following=deepcopy(sample);following.update(visual={},inspection_frame=None,inspection_key=None)
        following['events'].append({'at':time.time()+1,'arm':'arm2','run_id':sample['runs'][0]['run_id'],'sequence':6,'status':'A_LINEAR_STARTED:target_mm=100'})
        following['frame_received']=now+1.5;following['received']=now+1.5
        with patch('desktop_monitor.ui.time.monotonic',return_value=now+1.5):
            p.consume(following);self.assertTrue(p.canvas.find_withtag('frame'))
            self.assertIn('판정 영상',p.camera_status.get());self.assertIn('정상',p.camera_status.get())
        with patch('desktop_monitor.ui.time.monotonic',return_value=now+2.1):
            p.paint();self.assertTrue(p.camera_status.get().startswith('실시간'))
            self.assertFalse(any('실시간' in p.canvas.itemcget(item,'text') for item in p.canvas.find_withtag('annotation') if p.canvas.type(item)=='text'))

    def test_keep_view_pins_exact_inspection_and_new_job_cannot_inherit_it(self):
        p=self.panel();sample=self.passed();p.consume(sample);p.toggle_evidence()
        self.assertIs(p.pinned_inspection,sample)
        newer=deepcopy(sample);newer['inspection_key']=('v',2);newer['visual']['sequence']=2
        p.consume(newer);self.assertIs(p.evidence_snapshot(sample['runs'][0],time.monotonic()+60),sample)
        changed=deepcopy(newer);changed['runs'][0]['job_id']='new-job';changed['visual']={};changed['inspection_frame']=None
        p.consume(changed);self.assertIsNone(p.pinned_inspection);self.assertTrue(p.canvas.find_withtag('frame'));self.assertIn('직전 검사 결과',p.camera_status.get())

    def test_old_record_does_not_trigger_automatic_hold_or_extend_it_on_poll(self):
        p=self.panel();sample=self.passed();sample['visual']['age_s']=20;sample['events'][-1]['at']=sample['at']-20
        p.consume(sample);self.assertEqual(p.hold_until,0)
        sample=self.passed();p.consume(sample);deadline=p.hold_until
        p.consume(deepcopy(sample));self.assertEqual(p.hold_until,deadline)

    def test_recording_uses_live_frame_and_survives_disconnect_until_manual_stop(self):
        p=self.panel();sample=self.passed();sample['frame']=Image.new('RGB',(320,180),'blue')
        sample['inspection_frame']=Image.new('RGB',(320,180),'red');p.consume(sample);p.toggle_evidence()
        observer=p.observer
        with patch('desktop_monitor.ui.PreviewRecording') as factory:
            recorder=factory.return_value;recorder.done.is_set.return_value=False
            recorder.stopping.is_set.return_value=False;recorder.started=time.monotonic()
            p.toggle_recording()
            first=recorder.offer.call_args_list[0].args[0]
            self.assertEqual(first.getpixel((0,0)),(0,0,255));self.assertIs(p.observer,observer)
            self.assertEqual(p.record_button.cget('text'),'녹화 종료')
            p.consume({'connected':False,'error':'network'})
            recorder.finish.assert_not_called();self.assertIn('수신 대기',p.recording_status.get())
            p.toggle_recording();recorder.finish.assert_called_once();self.assertEqual(p.recording_reason,'직접 종료')
            recorder.done.is_set.return_value=True;recorder.error=None;recorder.path=Path('clip_original.mp4');recorder.name='clip'
            p.update_recording();self.assertIsNone(p.recorder);self.assertIn('저장 완료',p.recording_status.get())
            self.assertTrue(p.record_button.instate(['disabled']))

    def test_stop_view_keeps_recording_and_stale_video_cannot_start(self):
        p=self.panel();sample=visuals.visual_sample();sample['frame_age']=4;p.consume(sample)
        with patch('desktop_monitor.ui.PreviewRecording') as factory:
            p.toggle_recording();factory.assert_not_called()
        recorder=Mock();recorder.stopping.is_set.return_value=False;recorder.done.is_set.return_value=False
        recorder.started=time.monotonic();recorder.fps=10;p.recorder=recorder
        p.deactivate(force=True);recorder.finish.assert_not_called();self.assertIn('수신 대기',p.recording_status.get())
        p.recorder=None

    def test_maximized_controls_and_labels_stay_outside_video(self):
        p=self.panel();p.consume(self.passed());self.root.update()
        text=' '.join(p.canvas.itemcget(item,'text') for item in p.canvas.find_withtag('annotation') if p.canvas.type(item)=='text')
        self.assertNotIn('판정 영상',text);self.assertNotIn('실시간',text)
        self.assertIn('촬영',p.camera_status.get())
        for widget in (p.record_button,p.record_folder_button,p.inspection_button,p.zoom_button):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        from PIL import ImageGrab
        import os
        output=Path(__file__).resolve().parents[1]/'verification/work-monitor-hold-recording-20261027/inspection-hold.png'
        output.parent.mkdir(parents=True,exist_ok=True)
        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(output)


class ObserverRateTests(unittest.TestCase):
    def test_five_fps_preview_does_not_multiply_execution_record_queries(self):
        from desktop_monitor.source import Source
        with TemporaryDirectory() as folder:
            source=Source(folder)
            source.control=Mock(return_value={'info':None});source.records=Mock(return_value=([],[],[]))
            source.display_names=Mock(return_value={});source.camera=Mock(return_value={});source.visual=Mock(return_value={})
            for index in range(10):
                with patch('desktop_monitor.source.time.monotonic',return_value=100+index*.2):source.snapshot()
            self.assertEqual(source.camera.call_count,10)
            self.assertLessEqual(source.control.call_count,4);self.assertEqual(source.control.call_count,source.records.call_count)
    def test_preview_subscription_renews_once_per_second_without_delaying_release(self):
        from desktop_monitor.source import Source
        import io,json
        with TemporaryDirectory() as folder,patch('desktop_monitor.source.urlopen') as opened:
            opened.side_effect=lambda *a,**k:io.BytesIO(json.dumps({'ok':True}).encode())
            source=Source(folder);info={'port':12345,'instance':'fixture'}
            for now in (100,100.2,100.4,100.6,100.8,101.01):
                with patch('desktop_monitor.source.time.monotonic',return_value=now):source.preview_request('arm2',info,'test','watch')
            self.assertEqual(opened.call_count,2)
            source.release_preview();self.assertEqual(opened.call_count,3)
            self.assertEqual(source.watched,{})
