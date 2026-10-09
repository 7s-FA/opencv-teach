"""Read-only V4L2 discovery: never start a stream or change device settings."""
import errno
import os
from pathlib import Path
import struct

COLOR_FORMATS={'YUYV','UYVY','MJPG','JPEG','RGB3','BGR3','NV12','NV21','YU12','YV12','RGB4','BGR4'}

def probe(device):
    import fcntl
    # Linux videodev2.h: v4l2_capability (104 bytes), v4l2_fmtdesc (64).
    fd=os.open(device,os.O_RDONLY|os.O_NONBLOCK)
    try:
        cap=bytearray(104);fcntl.ioctl(fd,0x80685600,cap,True)
        if not bytes(cap[48:80]).startswith(b'usb-'):return None
        flags=struct.unpack_from('=I',cap,84)[0]
        if flags&0x80000000:flags=struct.unpack_from('=I',cap,88)[0]
        if flags&(0x00800000|0x4000|0x8000) or not flags&(1|0x1000):return None
        formats=[]
        for index in range(256):
            fmt=bytearray(64);struct.pack_into('=II',fmt,0,index,1 if flags&1 else 9)
            try:fcntl.ioctl(fd,0xc0405602,fmt,True)
            except OSError as exc:
                if exc.errno==errno.EINVAL:break
                raise
            formats.append(bytes(fmt[44:48]).decode('ascii',errors='replace').strip())
        # RealSense stereo IR advertises packed UYVY too; it is not the RGB sensor.
        if {'Z16','Y8I','Y12I'}.intersection(formats):return None
        if not COLOR_FORMATS.intersection(formats):return None
        return {'name':bytes(cap[16:48]).split(b'\0')[0].decode(errors='replace'),'formats':formats}
    finally:os.close(fd)

def camera_inventory(dev_root='/dev'):
    if os.name=='nt':return []
    root=Path(dev_root);aliases={}
    for directory in ('v4l/by-id','v4l/by-path'):
        for path in sorted((root/directory).glob('*')):
            aliases.setdefault(str(path.resolve()),str(path))
    result=[]
    for device in sorted(root.glob('video[0-9]*'),key=lambda p:int(p.name[5:])):
        try:info=probe(str(device))
        except OSError:continue  # Unplugged, unavailable or inaccessible device.
        if not info:continue
        source=aliases.get(str(device.resolve()),str(device));identity=(source+' '+info['name']).lower()
        name='D435' if 'realsense' in identity and '435' in identity else 'Innomaker' if 'innomaker' in identity else info['name']
        result.append({**info,'name':name,'source':source,'device':str(device),'kind':'color'})
    return result
