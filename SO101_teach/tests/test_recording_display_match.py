"""Recording must use the frame actually selected by the visible monitor."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch
from PIL import Image
from tests import test_work_monitor_recording as fixture
from tests.test_work_visuals import visual_sample

class RecordingDisplayMatchTests(unittest.TestCase):
    setUp=fixture.MonitorRetentionRecordingUITests.setUp
    tearDown=fixture.MonitorRetentionRecordingUITests.tearDown
    panel=fixture.MonitorRetentionRecordingUITests.panel
    passed=fixture.MonitorRetentionRecordingUITests.passed

    def test_pending_observation_must_not_flash_zoomed_frame_only_in_recording(self):
        p=self.panel();s=visual_sample()
        s['frame']=Image.new('RGB',(1280,720),'blue')
        s['inspection_frame']=Image.new('RGB',(960,540),'red')
        p.consume(s)
        self.assertTrue(p.camera_status.get().startswith('실시간'))
        view=p.recording_inspection(time.monotonic())
        self.assertEqual(view['image'].getpixel((1,1)),(0,0,255))
        self.assertEqual(view['heading']+'\n'+view['detail'],p.camera_status.get())

    def test_recording_respects_hidden_annotations_without_changing_zoom(self):
        p=self.panel();s=self.passed();p.consume(s)
        p.overlays.set(False);p.paint()
        view=p.recording_inspection(time.monotonic())
        self.assertEqual(view['checks'],[]);self.assertEqual(view['outlines'],[])
        self.assertLess(view['image'].width,s['inspection_frame'].width)
        self.assertIn('검사 영역 확대',view['heading'])

    def test_manual_return_to_live_cannot_reintroduce_recent_inspection_in_recording(self):
        p=self.panel();s=self.passed();s['frame']=Image.new('RGB',(1280,720),'blue')
        s['inspection_frame']=Image.new('RGB',(960,540),'red');p.consume(s)
        p.toggle_evidence();p.toggle_evidence()
        self.assertTrue(p.camera_status.get().startswith('실시간'))
        view=p.recording_inspection(time.monotonic())
        self.assertEqual(view['image'].getpixel((1,1)),(0,0,255))

    def test_encoded_video_keeps_full_display_hold_through_handoff_without_pending_flashes(self):
        import cv2,json,shutil
        from pathlib import Path
        from desktop_monitor.recording import PreviewRecording
        from PIL import ImageGrab
        import os
        p=self.panel();base=time.monotonic();sample=self.passed();wall=sample['at']
        sample['frame']=Image.new('RGB',(1280,720),'blue')
        sample['inspection_frame']=Image.new('RGB',(960,540),'red')
        out=Path(__file__).resolve().parents[1]/'verification/inspection-recording-review-20261027'
        out.mkdir(parents=True,exist_ok=True)
        recorder=PreviewRecording(self.data/'recordings',now=base,fps=10);recorder.start()
        frames=[]
        try:
            for index in range(36):
                now=base+index/10;s=deepcopy(sample)
                s.update(received=now,frame_received=now,frame_key=('camera',index),at=wall+index/10)
                if index<5:s['events']=s['events'][:-1]  # Observation before final PASS.
                else:s['events'][-1]['at']=wall+.5
                if index>=10:
                    s['runs'][0]['job_id']='next-job';s['visual']={};s['inspection_frame']=None
                with patch('desktop_monitor.ui.time.monotonic',return_value=now):
                    p.consume(s)
                    view=p.recording_inspection(now)
                    self.assertEqual(view['heading']+'\n'+view['detail'],p.camera_status.get())
                    recorder.offer(p.frame,now,inspection=p.recording_inspection)
                    if index==10:
                        self.root.update_idletasks()
                        ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(out/'displayed-hold.png')
                frames.append(view['image'].getpixel((1,1))==(255,0,0))
                time.sleep(.025)  # Let the actual encoder consume its bounded queue.
        finally:
            recorder.finish();recorder.join(5)
        self.assertIsNone(recorder.error);self.assertEqual([i for i,v in enumerate(frames) if v],list(range(5,25)))
        decoded=[];capture=cv2.VideoCapture(str(recorder.inspection_path))
        try:
            self.assertEqual(capture.get(cv2.CAP_PROP_FRAME_COUNT),36)
            self.assertEqual((capture.get(3),capture.get(4)),(960,540))
            while True:
                ok,frame=capture.read()
                if not ok:break
                decoded.append(bool(frame[270,480,2]>200 and frame[270,480,0]<20))
                if len(decoded)==11:cv2.imwrite(str(out/'encoded-hold.png'),frame)
        finally:capture.release()
        self.assertEqual(decoded,frames)
        shutil.copy2(recorder.path,out/'timeline_original.mp4')
        shutil.copy2(recorder.inspection_path,out/'timeline_inspection.mp4')
        (out/'encoded-timeline.json').write_text(json.dumps({'fps':10,'frames':36,'inspection_size':[960,540],
            'displayed_hold_frame_indices':[i for i,v in enumerate(frames) if v],
            'encoded_hold_frame_indices':[i for i,v in enumerate(decoded) if v],
            'hold_seconds':sum(decoded)/10,'pending_inspection_flashes':sum(decoded[:5]),
            'robot_motion_commands':0},indent=2))
