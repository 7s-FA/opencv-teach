"""Draw saved decision metadata on its received frame; never run detection again."""
import cv2
import numpy as np
from .inspection_overlay import draw_label
from .vision import draw_outline,project_plane
from .height_reference import detection_plane_z,support_plane_profile
from .display_coordinates import display_position,display_heading


def polygon(value):
    if value is None:return None
    points=np.asarray(value,float)
    if (points.ndim!=2 or points.shape[1]!=2 or not 3<=len(points)<=4096
        or not np.isfinite(points).all() or np.abs(points).max()>100000):return None
    return points


def label(image,text,point,*,below=False):
    # Keep metadata legible without expanding or resizing the evidence frame.
    limit=max(4,(image.shape[1]-20)//22)
    text=str(text).replace('\n',' ')
    if len(text)>limit:text=text[:max(1,limit-1)]+'…'
    draw_label(image,text,point,below=below)


def render(raw,*,source,context,role,inspection=None,error=''):
    frame=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
    if frame is None:raise ValueError('원본 JPEG 디코딩 실패')
    image=frame.copy();report={'decision_rows':0,'confirmed_jigs':0,'detected_jigs':0,'warnings':[]}
    detection=source.get('detection') or {}
    results=detection.get('by_jig') or {}
    if not results and detection.get('selected'):results={'jig':detection}
    for key,result in results.items():
        chosen=result.get('selected') or {};points=polygon(chosen.get('outline_px',chosen.get('quad')))
        if points is not None:
            draw_outline(image,points,(230,200,45));report['detected_jigs']+=1
            name=(context.get('jigs',{}).get(key) or {}).get('name',key)
            label(image,'검출 · '+name,points.min(axis=0),below=True)
    profile=context.get('profile') or {};fixed=source.get('confirmed_jigs') or context.get('fixed_jigs') or {}
    for key,reference in fixed.items():
        try:
            if list((profile.get('intrinsics') or {}).get('size',[]))!=[frame.shape[1],frame.shape[0]]:
                raise ValueError('프레임 크기와 카메라 보정 불일치')
            config=context['jigs'][key];z=detection_plane_z(support_plane_profile(profile,config),config['mesh'])
            x,y,yaw=reference['pose'];angle=np.radians(yaw);origin=np.array([x,y])
            points=np.array([origin,origin+20*np.array([np.cos(angle),np.sin(angle)]),origin+20*np.array([-np.sin(angle),np.cos(angle)])])
            camera=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera'],float))
            xyz=np.c_[points,np.full(3,z)]@camera[:3,:3].T+camera[:3,3]
            if not np.isfinite(xyz).all() or np.any(xyz[:,2]<=0):raise ValueError('확정 지그가 카메라 뒤쪽')
            uv=project_plane(points,profile,z)
            if not np.isfinite(uv).all() or np.abs(uv).max()>100000:raise ValueError('확정 지그 투영 범위 오류')
            c,a,b=[tuple(np.rint(v).astype(int)) for v in uv]
            if not (0<=c[0]<image.shape[1] and 0<=c[1]<image.shape[0]):raise ValueError('확정 지그 중심이 영상 밖')
            cv2.drawMarker(image,c,(40,220,100),cv2.MARKER_CROSS,18,2)
            cv2.arrowedLine(image,c,a,(40,220,100),2,tipLength=.25)
            cv2.line(image,c,b,(40,220,100),2)
            dx,dy=display_position([x,y]);heading=display_heading(yaw,reference.get('symmetry_deg',90))
            label(image,f'확정 {config.get("name",key)} · X {dx:.1f} Y {dy:.1f} / {heading:.1f}°',c,below=True)
            report['confirmed_jigs']+=1
        except (KeyError,TypeError,ValueError,cv2.error,np.linalg.LinAlgError) as exc:
            report['warnings'].append(key+': '+str(exc))
    observation=(inspection or {}).get('result') or {}
    matched=role=='inspection' and observation.get('at')==source['captured_monotonic']
    rows=[]
    if matched:
        if isinstance(observation.get('row'),dict):rows.append(observation['row'])
        for result in (observation.get('results') or {}).values():
            if (isinstance(result,dict) and isinstance(result.get('row'),dict)
                and result.get('at',observation.get('at'))==source['captured_monotonic']):rows.append(result['row'])
    for index,row in enumerate(rows):
        color=(40,200,90) if row.get('quality')=='normal' else (45,65,235)
        regions=[]
        for name in ('roi_display_polygon','roi_polygon','part_top_polygon','observed_polygon'):
            points=polygon(row.get(name))
            if points is not None:
                draw_outline(image,points,color,dashed=name.startswith('roi'))
                regions.append(points)
                if name=='roi_display_polygon':break
        for name in ('part_top_polygon','observed_polygon'):
            points=polygon(row.get(name))
            if points is not None:draw_outline(image,points,color)
        title=(row.get('label') or row.get('state') or '판정 없음')+' · '+(row.get('reason') or '')
        point=regions[0].max(axis=0) if regions else (10,80+index*36)
        label(image,title,point,below=True);report['decision_rows']+=1
    title={'inspection':'검사 판정 · 같은 프레임','jig-confirmed':'실행에 사용한 확정 지그','detection':'지그 검출 결과','last-received':'마지막 수신 · 판정 프레임 아님'}.get(role,role)
    if matched and not rows:title+=' · '+str(observation.get('error') or observation.get('reason') or '결과 없음')
    label(image,title,(10,38))
    if error:label(image,'오류: '+error,(10,image.shape[0]-8))
    ok,jpeg=cv2.imencode('.jpg',image,[cv2.IMWRITE_JPEG_QUALITY,92])
    if not ok:raise ValueError('표시 영상 JPEG 저장 실패')
    return jpeg.tobytes(),report
