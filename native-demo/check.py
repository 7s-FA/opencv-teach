"""Check real Tk startup, rendering and explicit offline device modes."""
import json,time,urllib.request
for _ in range(120):
    try:
        with urllib.request.urlopen('http://127.0.0.1:6080/health',timeout=2) as r:data=json.load(r)
        if data.get('ready') and data.get('rendered'):
            assert data['kind']=='native-python-tk'
            assert data['motor_modes']==['demo','demo']
            assert data['camera']=='saved-faults-photo'
            print(json.dumps(data,ensure_ascii=False));break
    except (OSError,ValueError):pass
    time.sleep(.5)
else:raise SystemExit('Original GUI did not become ready with a rendered frame within 60 seconds.')
