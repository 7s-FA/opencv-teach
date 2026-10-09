"""Run native UI tests in Xephyr without mapping a window on the user's desktop."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time

root=Path(__file__).resolve().parents[1]
x=ctypes.CDLL('libX11.so.6')
x.XOpenDisplay.restype=ctypes.c_void_p
x.XDefaultRootWindow.argtypes=[ctypes.c_void_p];x.XDefaultRootWindow.restype=ctypes.c_ulong
x.XCreateSimpleWindow.argtypes=[ctypes.c_void_p,ctypes.c_ulong,ctypes.c_int,ctypes.c_int,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_ulong,ctypes.c_ulong]
x.XCreateSimpleWindow.restype=ctypes.c_ulong
x.XFlush.argtypes=[ctypes.c_void_p];x.XCloseDisplay.argtypes=[ctypes.c_void_p]
d=x.XOpenDisplay(None)
if not d:raise SystemExit('UI 테스트용 X 디스플레이에 연결할 수 없습니다.')
server=None
try:
    window=x.XCreateSimpleWindow(d,x.XDefaultRootWindow(d),0,0,1920,1080,0,0,0)
    x.XFlush(d)  # Deliberately never map the parent window.
    number=next(n for n in range(110,160) if not Path(f'/tmp/.X11-unix/X{n}').exists() and not Path(f'/tmp/.X{n}-lock').exists())
    display=f':{number}'
    server=subprocess.Popen(['Xephyr',display,'-parent',str(window),'-screen','1920x1080','-ac','-noreset'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for _ in range(60):
        if server.poll() is not None:raise RuntimeError('백그라운드 테스트 화면 시작 실패')
        if Path(f'/tmp/.X11-unix/X{number}').exists():break
        time.sleep(.05)
    else:raise RuntimeError('백그라운드 테스트 화면 시간 초과')
    result=subprocess.run(['bash',str(root/'run_tests.sh'),*sys.argv[1:]],env={**os.environ,'DISPLAY':display})
    raise SystemExit(result.returncode)
finally:
    if server:
        server.terminate()
        try:server.wait(5)
        except subprocess.TimeoutExpired:server.kill();server.wait()
    x.XCloseDisplay(d)
