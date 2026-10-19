"""Boundaries of the image-only worker protocol and mask conflict filtering."""
import io
import json
import struct
import unittest
import numpy as np
from so101_teach.yolo_worker import send_packet,receive_packet,confidence_value,MAX_IMAGE_BYTES
from so101_teach.yolo_inference import deduplicate_classes

class ShortStream(io.BytesIO):
    def write(self,data):return super().write(data[:7])
    def read(self,size=-1):return super().read(min(size,11))

class YoloProtocolTests(unittest.TestCase):
    def test_lossless_packets_survive_partial_io(self):
        frame=np.arange(11*17*3,dtype=np.uint8).reshape(11,17,3)
        for codec in ('raw','png'):
            stream=ShortStream();send_packet(stream,{'kind':'infer','confidence':.7},frame,codec)
            stream.seek(0);result=receive_packet(stream)
            np.testing.assert_array_equal(result['image'],frame)
            self.assertEqual(result['confidence'],.7)
    def test_truncated_and_oversized_packets_are_rejected(self):
        with self.assertRaises(EOFError):receive_packet(io.BytesIO(b'\x00\x00'))
        with self.assertRaises(ValueError):receive_packet(io.BytesIO(struct.pack('!I',1048577)))
        for fields in ({'image_bytes':MAX_IMAGE_BYTES+1}, {'image_bytes':3,'image_shape':[1,2,3],'image_codec':'raw'},
                       {'image_bytes':3,'image_shape':[1,1,4],'image_codec':'raw'}):
            header=json.dumps(fields).encode();stream=io.BytesIO(struct.pack('!I',len(header))+header+b'abc')
            with self.assertRaises(ValueError):receive_packet(stream)
    def test_threshold_choices_are_strict(self):
        for value in (.5,.6,.7,.8,.9):self.assertEqual(confidence_value(str(value)),value)
        for value in (.4,1.,float('nan'),.55):
            with self.assertRaises(ValueError):confidence_value(value)
    def test_conflicting_masks_require_both_overlap_and_margin(self):
        masks=np.ones((4,8,8),bool)
        result={'scores':np.array([.9,.85,.89,.8]),'classes':np.array([1,2,1,0]),'masks':masks,'boxes':np.zeros((4,4))}
        filtered=deduplicate_classes(result,.5)
        self.assertEqual(filtered['classes'].tolist(),[1,1,0])
        result['scores'][1]=.895
        self.assertEqual(len(deduplicate_classes(result,.5)['scores']),4)
    def test_pc_submission_matches_ten_fps_camera_without_backlog(self):
        import queue
        from unittest.mock import patch
        from so101_teach.yolo_worker import YoloWorker,PC_MAX_FPS
        worker=YoloWorker();worker.ready=True;worker.requests=queue.Queue(maxsize=1);worker.max_fps=PC_MAX_FPS
        frame=np.zeros((2,2,3),np.uint8)
        with patch('so101_teach.yolo_worker.time.monotonic',return_value=10.):
            self.assertTrue(worker.submit(frame,1.,.5))
            self.assertFalse(worker.submit(frame,2.,.5))
        worker.requests.get_nowait();worker.busy=False
        with patch('so101_teach.yolo_worker.time.monotonic',return_value=10.09):
            self.assertFalse(worker.submit(frame,2.,.5))
        with patch('so101_teach.yolo_worker.time.monotonic',return_value=10.1):
            self.assertTrue(worker.submit(frame,2.,.5))
        self.assertEqual(worker.requests.qsize(),1)
