"""A recorder survives preview gaps and navigation until explicitly stopped."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time,unittest
from unittest.mock import Mock,patch
from PIL import Image
from desktop_monitor.recording import PreviewRecording
from tests import test_work_monitor_recording as fixture

class RecordingContinuityUITests(unittest.TestCase):
    setUp=fixture.MonitorRetentionRecordingUITests.setUp
    tearDown=fixture.MonitorRetentionRecordingUITests.tearDown
    panel=fixture.MonitorRetentionRecordingUITests.panel
    passed=fixture.MonitorRetentionRecordingUITests.passed

    def install_recorder(self,p):
        recorder=Mock();recorder.done.is_set.return_value=False;recorder.stopping.is_set.return_value=False
        recorder.started=time.monotonic();recorder.fps=10;p.recorder=recorder
        return recorder

    def test_stale_or_disconnected_preview_does_not_finish_recording(self):
        p=self.panel();s=self.passed();p.consume(s);r=self.install_recorder(p)
        s=deepcopy(s);s['frame_age']=3;p.consume(s)
        r.finish.assert_not_called();self.assertIn('수신 대기',p.recording_status.get())
        p.consume({'connected':False,'error':'temporary read timeout'})
        r.finish.assert_not_called();self.assertEqual(p.record_button.cget('text'),'녹화 종료')
        p.recorder=None

    def test_leaving_camera_page_keeps_recording_and_observer_alive(self):
        p=self.panel();p.consume(self.passed());r=self.install_recorder(p);observer=p.observer
        self.app.show_page('teach');self.root.update_idletasks()
        r.finish.assert_not_called();self.assertIs(p.observer,observer);self.assertFalse(observer.stop.is_set())
        p.recorder=None

class RecordingContinuityEncoderTests(unittest.TestCase):
    def test_long_offer_gap_does_not_automatically_stop(self):
        with TemporaryDirectory() as folder:
            r=PreviewRecording(folder,now=0,fps=2);r.offer(Image.new('RGB',(64,48),'blue'),0)
            r.offer(Image.new('RGB',(64,48),'red'),5)
            self.assertIsNone(r.error);self.assertFalse(r.stopping.is_set())
            r.finish();r.start();r.join(3);self.assertIsNone(r.error);self.assertEqual(r.frames,11)

class RecordingRecoveryVideoTests(unittest.TestCase):
    setUp=fixture.MonitorRetentionRecordingUITests.setUp
    tearDown=fixture.MonitorRetentionRecordingUITests.tearDown
    panel=fixture.MonitorRetentionRecordingUITests.panel

    def test_manual_stop_preserves_gap_duration_and_resumes_both_movies(self):
        import cv2,json,shutil,os
        from PIL import ImageGrab
        from tests.test_work_visuals import visual_sample
        p=self.panel();base=time.monotonic();sample=visual_sample();sample['visual']={};sample['inspection_frame']=None
        sample['frame']=Image.new('RGB',(640,360),'blue');sample['received']=base;sample['frame_received']=base
        out=Path(__file__).resolve().parents[1]/'verification/recording-until-manual-stop-20261027'
        out.mkdir(parents=True,exist_ok=True)
        with patch('desktop_monitor.ui.time.monotonic',return_value=base):p.consume(sample);p.toggle_recording()
        recorder=p.recorder;observer=p.observer
        self.assertIsNotNone(recorder)
        for index in range(1,120):
            now=base+index/10;s=deepcopy(sample);s.update(received=now,frame_received=now,frame_key=('camera',index))
            if 30<=index<50:s['frame']=None;s['camera']['running']=False
            if 50<=index<80:s={'connected':False,'error':'temporary read timeout','received':now}
            if index>=80:
                s['frame']=Image.new('RGB',(640,360),'green');s['runs'][0]['job_id']='following-arm'
            with patch('desktop_monitor.ui.time.monotonic',return_value=now):
                p.consume(s)
                if index==45:
                    self.root.update_idletasks();ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(out/'waiting-recording.png')
                if index==55:self.app.show_page('teach');self.root.update_idletasks()
                if index==90:self.app.show_page('camera');self.root.update_idletasks()
                self.assertIs(p.recorder,recorder);self.assertFalse(recorder.stopping.is_set())
            time.sleep(.015)
        self.assertIs(p.observer,observer)
        with patch('desktop_monitor.ui.time.monotonic',return_value=base+12):p.toggle_recording()
        recorder.join(5);self.assertTrue(recorder.done.is_set());self.assertIsNone(recorder.error)
        p.update_recording();self.assertIsNone(p.recorder);self.assertIn('직접 종료',p.recording_status.get())
        results=[]
        for path in (recorder.path,recorder.inspection_path):
            cap=cv2.VideoCapture(str(path));decoded=[]
            try:
                self.assertEqual((cap.get(3),cap.get(4),cap.get(5)),(640,360,10))
                while True:
                    ok,frame=cap.read()
                    if not ok:break
                    decoded.append(frame)
            finally:cap.release()
            self.assertEqual(len(decoded),120)
            self.assertGreater(decoded[10][10,10,0],200)
            self.assertGreater(decoded[60][10,10,0],200);self.assertLess(decoded[60][10,10,2],20)
            self.assertGreater(decoded[85][10,10,1],90);self.assertLess(decoded[85][10,10,0],20)
            self.assertGreater(decoded[-1][10,10,1],90)
            shutil.copy2(path,out/path.name);results.append({'file':path.name,'frames':len(decoded),'duration_seconds':12})
            cv2.imwrite(str(out/('gap-'+path.stem.split('_')[-1]+'.jpg')),decoded[60])
        events=[json.loads(line) for line in recorder.journal_path.read_text().splitlines()]
        self.assertEqual([e['state'] for e in events if e['event']=='source'],['live','waiting_frame','disconnected','live'])
        self.assertEqual(events[-1]['reason'],'직접 종료');self.assertEqual(events[-1]['duration_seconds'],12)
        shutil.copy2(recorder.journal_path,out/recorder.journal_path.name)
        (out/'recovery-video-result.json').write_text(json.dumps({'movies':results,'source_events':events,
            'background_observer_preserved':True,'gap_filled_with_last_frame':True,'physical_robot_commands':0},ensure_ascii=False,indent=2))

class DelayedFrameTimingTests(unittest.TestCase):
    def test_slow_delivery_and_stop_between_frames_keep_elapsed_time_and_pixels(self):
        import cv2,json
        with TemporaryDirectory() as folder:
            r=PreviewRecording(folder,now=100,fps=2)
            r.offer(Image.new('RGB',(320,180),'blue'),100)
            r.offer(Image.new('RGB',(320,180),'red'),105)
            r.finish(now=108,reason='직접 종료');r.start();r.join(5)
            self.assertIsNone(r.error);self.assertEqual(r.frames,16)
            for path in (r.path,r.inspection_path):
                cap=cv2.VideoCapture(str(path));frames=[]
                try:
                    self.assertEqual(cap.get(5),2)
                    while True:
                        ok,frame=cap.read()
                        if not ok:break
                        frames.append(frame)
                finally:cap.release()
                self.assertEqual(len(frames),16)
                self.assertTrue(all(f[10,10,0]>200 and f[10,10,2]<20 for f in frames[:10]))
                self.assertTrue(all(f[10,10,2]>200 and f[10,10,0]<20 for f in frames[10:]))
            log=[json.loads(line) for line in r.journal_path.read_text().splitlines()]
            self.assertEqual(log[-1]['duration_seconds'],8)
            self.assertTrue(any(e['event']=='timer_gap' for e in log))
