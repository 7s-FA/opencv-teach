"""Demo-only I/O boundary. Real hardware and network transports are unavailable."""
import os,sys

def blocked(*args,**kwargs):
    raise RuntimeError('다운로드 데모에서는 실제 장치·네트워크 연결을 사용할 수 없습니다.')

def install():
    import serial,cv2
    import so101_teach.devices as devices
    import so101_teach.camera_inventory as inventory
    devices.ensure_available=blocked;devices.new_bus=blocked
    serial.Serial.open=blocked
    cv2.VideoCapture=blocked
    inventory.camera_inventory=lambda *a,**k:[]
    def audit(event,args):
        if event in ('socket.connect','socket.bind','socket.getaddrinfo'):blocked()
        if event=='subprocess.Popen':
            executable,argv=args[:2]
            parts=[str(v) for v in argv] if not isinstance(argv,str) else [argv]
            # The original renderer uses Python multiprocessing, including its tracker.
            child=os.path.normcase(os.path.abspath(str(executable)))==os.path.normcase(os.path.abspath(sys.executable))
            worker=any('--multiprocessing-fork'==v or 'multiprocessing.resource_tracker' in v or 'multiprocessing.spawn' in v for v in parts)
            if not(child and worker):blocked()
    sys.addaudithook(audit)
