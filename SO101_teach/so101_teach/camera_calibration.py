"""Offline OpenCV calibration; call from a worker, never from the motor loop."""
from pathlib import Path
import cv2,numpy as np
from scipy.spatial.transform import Rotation

def board_points(cols=13,rows=9,square_mm=20.):
    cols=int(cols);rows=int(rows);square_mm=float(square_mm)
    if not 3<=cols<=40 or not 3<=rows<=40 or not 0<square_mm<=1000:raise ValueError('체커 내부 코너 수·간격을 확인하세요.')
    obj=np.zeros((cols*rows,3),np.float32);obj[:,:2]=np.mgrid[:cols,:rows].T.reshape(-1,2)*square_mm;return obj

def corners(frame,cols,rows):
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    ok,p=cv2.findChessboardCornersSB(gray,(int(cols),int(rows)),flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not ok:raise ValueError('체커보드 내부 코너를 찾지 못했습니다.')
    return p

def calibrate_images(paths,cols=13,rows=9,square_mm=20.,progress=None):
    obj=board_points(cols,rows,square_mm);objects=[];images=[];accepted=[];rejected=[];size=None
    for i,path in enumerate(paths):
        try:
            frame=cv2.imread(str(path))
            if frame is None:raise ValueError('사진 읽기 실패')
            current=frame.shape[1::-1]
            if size is not None and current!=size:raise ValueError('해상도 다름')
            pts=corners(frame,cols,rows);size=current;objects.append(obj.copy());images.append(pts);accepted.append(str(Path(path).resolve()))
        except ValueError as exc:rejected.append({'path':str(path),'reason':str(exc)})
        if progress:progress(i+1,len(paths))
    if len(images)<3:raise ValueError(f'코너가 검출된 같은 해상도 사진이 3장 이상 필요합니다. 현재 {len(images)}장')
    rms,K,D,rvecs,tvecs=cv2.calibrateCamera(objects,images,size,None,None)
    errors=[]
    for pts,rv,tv in zip(images,rvecs,tvecs):
        projected,_=cv2.projectPoints(obj,rv,tv,K,D);errors.append(float(np.sqrt(np.mean(np.sum((projected-pts)**2,axis=2)))))
    return {'K':K.tolist(),'D':D.ravel().tolist(),'size':list(size),'rms_px':float(rms),'errors_px':errors,'source_files':accepted,'rejected':rejected,
            'source_directory':str(Path(accepted[0]).parent),
            'board':{'cols':int(cols),'rows':int(rows),'square_mm':float(square_mm)}}

def validate_intrinsics(data):
    K=np.asarray(data['K'],float);D=np.asarray(data['D'],float).ravel();size=data['size']
    if K.shape!=(3,3) or not np.isfinite(K).all() or len(D) not in (4,5,8,12,14) or not np.isfinite(D).all() or K[0,0]<=0 or K[1,1]<=0 or not np.allclose(K[2],[0,0,1]) or len(size)!=2 or any(type(v) is not int or v<1 for v in size):raise ValueError('카메라 보정 형식 오류')
    return data

def board_extrinsics(frame,intrinsics,origin_mm,yaw_deg,cols=13,rows=9,square_mm=20.,row_sign=-1):
    validate_intrinsics(intrinsics);obj=board_points(cols,rows,square_mm);pts=corners(frame,cols,rows)
    if list(frame.shape[1::-1])!=intrinsics['size']:raise ValueError('카메라 영상과 보정 해상도가 다릅니다.')
    origin=np.asarray(origin_mm,float)
    if origin.shape!=(3,) or not np.isfinite(origin).all() or not np.isfinite(float(yaw_deg)) or row_sign not in (-1,1):raise ValueError('체커 원점·방향을 확인하세요.')
    ok,rv,tv=cv2.solvePnP(obj,pts,np.array(intrinsics['K']),np.array(intrinsics['D']))
    if not ok:raise ValueError('체커 좌표 연결 계산 실패')
    camera_board=np.eye(4);camera_board[:3,:3]=cv2.Rodrigues(rv)[0];camera_board[:3,3]=tv.ravel()
    base_board=np.eye(4);base_board[:3,:3]=Rotation.from_euler('z',yaw_deg,degrees=True).as_matrix()@np.diag([1,row_sign,row_sign]);base_board[:3,3]=origin
    T=base_board@np.linalg.inv(camera_board)
    return {'source':'measured_board','base_from_camera':T.tolist(),'origin_mm':origin.tolist(),'yaw_deg':float(yaw_deg),'row_sign':row_sign,'verified':False}
