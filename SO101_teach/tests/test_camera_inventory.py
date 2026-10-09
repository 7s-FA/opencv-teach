import errno
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from so101_teach.camera_inventory import probe,camera_inventory

class CameraInventoryTests(unittest.TestCase):
    def check_probe(self,flags,formats):
        def ioctl(fd,request,buffer,mutate):
            if request==0x80685600:
                buffer[16:20]=b'Test';buffer[48:52]=b'usb-';struct.pack_into('=II',buffer,84,0x80000000,flags)
            else:
                index=struct.unpack_from('=I',buffer)[0]
                if index>=len(formats):raise OSError(errno.EINVAL,'done')
                buffer[44:48]=formats[index]
        with patch('os.open',return_value=9),patch('os.close') as close,patch('fcntl.ioctl',side_effect=ioctl):
            result=probe('/dev/video4');close.assert_called_once_with(9);return result
    def test_only_color_capture_is_selectable(self):
        self.assertEqual(self.check_probe(1,[b'YUYV'])['formats'],['YUYV'])
        for flags,formats in [(1,[b'Z16 ']),(1,[b'GREY']),(1,[b'GREY',b'UYVY',b'Y8I ']),(0x00800000,[]),(0x4000|1,[b'YUYV'])]:
            self.assertIsNone(self.check_probe(flags,formats))
    def test_actual_rgb_node_and_stable_alias_are_used(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);aliases=root/'v4l/by-id';aliases.mkdir(parents=True)
            for i in (0,4,5):(root/f'video{i}').touch()
            alias=aliases/'usb-Intel_RealSense_435-video-index0';alias.symlink_to(root/'video4')
            def fake(device):return {'name':'Intel RealSense','formats':['YUYV']} if device.endswith('video4') else None
            with patch('so101_teach.camera_inventory.probe',side_effect=fake):items=camera_inventory(d)
            self.assertEqual(len(items),1);self.assertEqual(items[0]['source'],str(alias));self.assertEqual(items[0]['name'],'D435')
