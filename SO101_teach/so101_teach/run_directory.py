"""Observe run-file changes without rescanning history on every refresh.

Linux directory events are advisory. Missing support, overflow or watch loss
always requests a complete rescan; readers still validate each changed file.
"""
import ctypes
import os
from pathlib import Path
import struct


class RunDirectoryWatch:
    def __init__(self,folder):self.folder=Path(folder);self.fd=None;self.identity=None
    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None
        self.identity=None
    def __del__(self):
        try:self.close()
        except OSError:pass
    def _open(self,identity):
        self.close();self.identity=identity
        try:
            library=ctypes.CDLL(None,use_errno=True)
            library.inotify_init1.argtypes=[ctypes.c_int];library.inotify_init1.restype=ctypes.c_int
            library.inotify_add_watch.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_uint32];library.inotify_add_watch.restype=ctypes.c_int
            fd=library.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
            if fd<0:return
            # In-place modifications/mtime changes as well as atomic replacements.
            if library.inotify_add_watch(fd,os.fsencode(self.folder),0x2|0x4|0x8|0x40|0x80|0x200|0x400|0x800)<0:os.close(fd);return
            self.fd=fd
        except (AttributeError,OSError):self.fd=None
    def poll(self):
        try:
            stat=self.folder.stat();identity=(stat.st_dev,stat.st_ino)
        except FileNotFoundError:
            self.close();return set(),True
        if identity!=self.identity:
            self._open(identity);return set(),True
        if self.fd is None:return set(),True
        changed=set();rescan=False;lost=False
        try:
            while True:
                try:raw=os.read(self.fd,65536)
                except BlockingIOError:break
                if not raw:rescan=True;break
                offset=0
                while offset<len(raw):
                    _,mask,_,length=struct.unpack_from('iIII',raw,offset);offset+=16
                    name=os.fsdecode(raw[offset:offset+length].split(b'\0',1)[0]);offset+=length
                    if mask&(0x4000|0x8000|0x400|0x800):rescan=True
                    if mask&0x8000:lost=True
                    if name.endswith('.json'):
                        changed.add(self.folder/name)
                        if mask&(0x40|0x200):rescan=True
        except (OSError,struct.error):self.close();rescan=True
        if lost:self.close()
        return changed,rescan
