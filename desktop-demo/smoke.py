"""Exercise the packaged GUI, renderer, photo detector and persistent episode store."""
import json,time,uuid
from copy import deepcopy

def run_smoke(root,manager,output,errors,change_photo,case):
    output.parent.mkdir(parents=True,exist_ok=True)
    checks={};started=time.monotonic();stage=0;first=manager.active
    def snapshot(label):
        from PIL import ImageGrab
        try:ImageGrab.grab().save(output.parent/(label+'.png'))
        except Exception as exc:checks[label+'_screenshot_error']=str(exc)
    def finish(error=None):
        if error:errors.append(str(error))
        output.write_text(json.dumps({'passed':not errors,'checks':checks,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        manager.close()
        root.after(10000,root.destroy)
    def tick():
        nonlocal stage
        try:
            if errors:return finish()
            if time.monotonic()-started>150:return finish('GUI verification timed out at stage '+str(stage))
            app=manager.apps[manager.active]
            if stage==0 and app.last_rgb is not None:
                checks['first_arm_3d']=True;snapshot('teaching')
                doc=deepcopy(app.episode);doc['id']=uuid.uuid4().hex;doc['name']='데모 저장 검증'
                file=app.store.save(doc)
                assert json.loads(file.read_text(encoding='utf-8'))['name']=='데모 저장 검증'
                from data import prepare_data
                from so101_teach.domain import ROOT
                prepare_data(ROOT,app.data_dir)
                assert file.exists() and json.loads(file.read_text(encoding='utf-8'))['id']==doc['id']
                file.unlink();checks['save_reload_preserved']=True
                manager.select(next(k for k in manager.apps if k!=first));stage=1
            elif stage==1 and app.last_rgb is not None:
                checks['second_arm_3d']=True
                app.show_page('camera');stage=2
            elif stage==2 and app.camera and app.camera.observation:
                if app.camera.error:raise RuntimeError(app.camera.error)
                data=app.camera.observation[1];live=data.get('live_by_jig',{})
                if len(live)==2 and all(v.get('selected') for v in live.values()) and all(v.get('selected') for v in data.get('by_jig',{}).values()):
                    checks['both_jigs_detected']={k:v['status'] for k,v in live.items()}
                    checks['both_jigs_adopted']=True;snapshot('camera')
                    change_photo('오안착 사진');stage=3;checks['camera_switch_at']=time.monotonic()
            elif stage==3 and time.monotonic()-checks['camera_switch_at']>4 and app.camera and app.camera.observation:
                live=app.camera.observation[1].get('live_by_jig',{})
                if any(v.get('status')=='orientation_unconfirmed' for v in live.values()):
                    checks['fault_photo_processed']=True
                    checks['motors']=[type(a.session).__name__ for a in manager.apps.values()]
                    assert checks['motors']==['DemoSession','DemoSession']
                    import socket,serial
                    from offline import blocked
                    for name,action in [('network',lambda:socket.create_connection(('127.0.0.1',9))),('serial',lambda:serial.Serial('DEMO'))]:
                        try:action()
                        except RuntimeError:checks[name+'_blocked']=True
                        else:raise AssertionError(name+' was not blocked')
                    return finish()
            root.after(250,tick)
        except Exception as exc:finish(exc)
    root.after(250,tick)
