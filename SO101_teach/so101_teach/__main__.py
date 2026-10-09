import argparse
import json
import time
from .domain import ROOT,load_profile,atomic_json,EpisodeStore

def main():
    p=argparse.ArgumentParser(description='SO-101 raw-tick teaching workspace')
    p.add_argument('--demo',action='store_true',help='Offline simulated servos; no USB')
    p.add_argument('--camera-only',action='store_true',help='Connect the Pi camera only; disable motor connection and motion')
    p.add_argument('--integration',metavar='RECIPES_JSON',help='Explicitly enable ROS 2 episode commands with this recipe map')
    p.add_argument('--integration-domain',type=int,default=40,help='ROS domain for the opt-in receiver (default 40)')
    p.add_argument('--minimized',action='store_true',help='Start minimized without raising the workspace window')
    p.add_argument('--setup',action='store_true',help='Open configuration page')
    p.add_argument('--check',action='store_true',help='Validate local configuration without opening devices')
    p.add_argument('--probe',action='store_true',help='Read motors and camera; no motor writes')
    p.add_argument('--no-camera',action='store_true');p.add_argument('--seconds',type=float,default=3.)
    p.add_argument('--save-pose',help='Save an actual fresh measured pose during a read-only probe')
    args=p.parse_args()
    if args.demo and args.integration:p.error('데모는 통합 실물 명령 수신과 함께 실행할 수 없습니다.')
    if args.camera_only and (args.demo or args.integration or args.probe):p.error('카메라 전용은 데모·통합 실행·모터 진단과 함께 사용할 수 없습니다.')
    profile,cal,ref=load_profile()
    if args.check:
        from .geometry import Kinematics
        print(json.dumps({'application':'SO101_teach','unit':'motor_ticks','servo_count':6,'calibration_sha256':cal.sha256,
            'model_reference_matches':True,'reference_tcp_mm':Kinematics(ref,tcp=profile.get('tcp')).fk(ref.middle)[:3,3].tolist(),
            'physical_motion_available':'explicit_ticks_with_optional_jig_correction','physical_motion_hardware_verified':False,'jig_compensated_motion_available':True,'device_access':False},ensure_ascii=False,indent=2));return
    if args.probe:
        from .devices import ReadOnlySession,CameraSession
        if not .2<=args.seconds<=60:raise ValueError('읽기 시간은 0.2~60초입니다.')
        out=ROOT/'data/diagnostics'/f'probe-{time.time_ns()}';out.mkdir(parents=True)
        s=ReadOnlySession(profile['port'],cal,audit_path=out/'motor-read.json');cam=None if args.no_camera else CameraSession(**profile['camera'])
        s.start()
        if cam:cam.start()
        try:
            deadline=time.monotonic()+max(args.seconds,1.5)
            while time.monotonic()<deadline:
                if s.error:raise RuntimeError(s.error)
                if cam and cam.error:raise RuntimeError(cam.error)
                time.sleep(.05)
            if not s.latest or not s.latest.fresh():raise RuntimeError('최신 모터 측정 실패')
            snap=s.latest
            result={'ticks':snap.ticks,'telemetry':snap.telemetry,'wall_time':snap.wall_time,'calibration_matches':snap.calibration_matches,'calibration_sha256':cal.sha256}
            if cam:
                import cv2
                if cam.frame is None or time.monotonic()-cam.frame_at>1:raise RuntimeError('최신 카메라 측정 실패')
                result['camera_shape']=list(cam.frame.shape);cv2.imwrite(str(out/'camera.jpg'),cam.frame)
            if args.save_pose:
                store=EpisodeStore(ROOT/'data/episodes',cal,profile.get('robot_id','arm2'));doc=store.new(args.save_pose);doc['steps']=[store.capture(snap,args.save_pose)];result['saved_episode']=str(store.save(doc))
            atomic_json(out/'snapshot.json',result);print(json.dumps({'result_directory':str(out),**result},ensure_ascii=False,indent=2))
        finally:
            s.close()
            if cam:cam.close()
            s.join(3)
            if cam:cam.join(3)
        return
    import tkinter as tk
    from .ui import show_ready_window
    from .arm_workspaces import ArmWorkspaces
    root=tk.Tk();root.withdraw();workspaces=ArmWorkspaces(root,auto_camera=not args.no_camera,auto_devices=not args.camera_only,mode_override='demo' if args.demo else None,camera_only=args.camera_only);app=workspaces.apps[workspaces.active]
    if args.integration:
        if not 0<=args.integration_domain<=232:raise ValueError('ROS 도메인 범위는 0~232입니다.')
        from .integration_transport import IntegrationBridge
        workspaces.integration=IntegrationBridge(workspaces,args.integration,domain=args.integration_domain)
        for window in workspaces.apps.values():window.root.title(window.root.title()+' · 통합 명령 수신')
    if args.setup:app.show_page('settings')
    show_ready_window(root,minimized=args.minimized)
    root.mainloop()

if __name__=='__main__':main()
