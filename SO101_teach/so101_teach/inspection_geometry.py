"""Camera anchors for adopted jigs and commanded, fixed linear endpoints."""
from copy import deepcopy
import cv2
import numpy as np
from .height_reference import support_bottom_z
from .jig_heading import mesh_yaw_offset
from .workcell_scene import carriage_position_mm
from .linear_state import direction_label
from .arm_workspace import world_from_base


def pose(origin,yaw):
    a=np.deg2rad(yaw);T=np.eye(4);T[:2,:2]=[[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]];T[:3,3]=origin
    return T


def project(points,anchor,profile):
    xyz=np.asarray(points,float)@anchor['camera_from_local'][:3,:3].T+anchor['camera_from_local'][:3,3]
    if not np.isfinite(xyz).all() or np.any(xyz[:,2]<=0):raise ValueError('카메라 뒤쪽 영역')
    uv=cv2.projectPoints(xyz,np.zeros(3),np.zeros(3),np.asarray(profile['intrinsics']['K'],float),np.asarray(profile['intrinsics']['D'],float))[0].reshape(-1,2)
    if not np.isfinite(uv).all() or np.abs(uv).max()>100000:raise ValueError('투영 범위 오류')
    return uv


def anchors_for(frame,results,adopted,catalog,profile,reference,station,product,placement,endpoint='명령 위치'):
    intrinsic=profile.get('intrinsics') or {}
    if list(intrinsic.get('size',[]))!=[frame.shape[1],frame.shape[0]] or not profile.get('extrinsics'):
        return [],'현재 영상 해상도에 맞는 카메라 보정 필요'
    a=intrinsic.get('camera',{}).get('source');b=profile.get('camera',{}).get('source')
    if a is not None and b is not None and str(a)!=str(b):return [],'현재 카메라와 보정 카메라가 다름'
    products=('A','B') if product=='전체' else (product,)
    rows=[]
    try:
        if station=='리니어 조립':
            stage=(placement or {}).get('linear_stage',{});endpoints=stage.get('endpoint_reference',{})
            if not all('carriage_x_mm' in endpoints.get(k,{}) for k in ('forward','retracted')):return [],'리니어 전후진 고정 좌표 없음'
            base_camera=stage.get('alignment',{}).get('base_from_camera_for_preview')
            if base_camera is None:return [],'리니어 카메라 기준 없음'
            camera_base=np.linalg.inv(np.asarray(base_camera,float))
            state=stage.get('startup_state',{})
            command=state.get('commanded_mm') if state.get('known') else state.get('last_commanded_mm')
            manual=endpoint!='명령 위치'
            if manual:command=endpoints['forward' if endpoint=='전진 목표' else 'retracted']['commanded_mm']
            commands=[command] if command is not None else [endpoints[k]['commanded_mm'] for k in ('forward','retracted')]
            for mm in commands:
                name=direction_label(stage,mm)
                provenance=('미리보기 · ' if manual else '' if state.get('known') else '마지막 명령 · ' if command is not None else '명령 미확인 · ')+name+' 목표'
                stage_pose=pose(stage['position_mm'],stage['yaw_deg'])
                x=carriage_position_mm({**stage,'stroke_mm':mm})
                for i,center in enumerate(reference['stations']['linear_assembly']['fixture_centers_carriage_mm']):
                    local=pose([center[0]+x,center[1],center[2]],0)
                    world=stage_pose@local
                    active=np.linalg.inv(world_from_base(profile))@world
                    rows.append({'id':f'linear_{mm:g}_{i}','label':('좌측 A용 팔레트' if i==0 else '우측 B용 팔레트'), 'source':provenance,'origin':active[:3,3].tolist(),'yaw':float(np.rad2deg(np.arctan2(active[1,0],active[0,0]))),
                                 'camera_from_local':camera_base@world,'templates':[f'linear_{g}_{s["id"]}' for g in ('A','B') for s in [*reference['assembly_profiles'][g]['stages'],{'id':'cap_only'}]],
                                 'inspection_allowed':command is not None,'product':product,'expected_product':'A' if i==0 else 'B','part':'assembly','jid':None,'outline':None})
            return rows,'리니어 고정 좌표 · '+rows[0]['source']+' · 실제 위치 피드백 없음'
        key='carrier' if station=='운반용 지그' else 'finished_pallet';spec=reference['stations'][key];jid=spec['jig_id']
        accepted=adopted.get(jid,{});result=accepted if accepted.get('selected') else results.get(jid,{})
        item=result.get('selected') or {};metric=item.get('metric')
        if not metric:return [],'기준 지그 감지·채택 대기'
        if not accepted.get('selected') and any(result.get(k) for k in ('pose_held','pose_frozen','teaching_held')):return [],'기준 지그 감지 대기'
        if key=='carrier' and (not item.get('orientation_verified') or metric.get('symmetry_deg')!=360):return [],'운반 지그 앞뒤 방향 확인 대기'
        yaw=float(metric['yaw_deg'])+mesh_yaw_offset(metric)
        base=pose([*metric['center_xy_mm'],support_bottom_z(catalog[jid],profile)],yaw)
        camera_base=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera'],float))
        source='채택 위치' if accepted.get('selected') else '현재 검출 위치'
        if result.get('inspection_only'):source=result.get('inspection_source','검사용 외곽 위치 · 제품 가림')
        if result.get('pose_held'):source+=' · 유지'
        if result.get('pose_frozen') or result.get('teaching_held'):source+=' · 고정'
        rois=[r for r in spec['rois'] if r['product'] in products] if key=='carrier' else [None]
        for row in rois:
            local=pose(row['fixture_center_xyz_mm'],0) if row else np.eye(4);world=base@local
            candidates=[f'carrier_{row["id"]}_{s}' for s in ('empty','present')] if row else [f'finished_{g}_{s["id"]}' for g in products for s in [*reference['assembly_profiles'][g]['stages'],{'id':'cap_only'}]]
            if row:candidates += [f'carrier_{row["id"]}_wrong_{g}_{k}' for g in ('A','B') for k in ('housing','insert','cap') if (g,k)!=(row['product'],row['part'])]
            rows.append({'id':row['id'] if row else 'finished','label':row['product']+' '+{'housing':'하단','insert':'중단','cap':'상단'}[row['part']] if row else '완성품 팔레트',
                         'source':source,'origin':world[:3,3].tolist(),'yaw':yaw,'camera_from_local':camera_base@world,'templates':candidates,
                         'inspection_allowed':True,'product':row['product'] if row else product,'part':row['part'] if row else 'finished','jid':jid,
                         'outline':item.get('outline_px',item.get('quad')),'jig_center':metric['center_xy_mm'],'jig_yaw':metric['yaw_deg']})
        return rows,source+' 기준 · 윗면 형상 비교'
    except (KeyError,TypeError,ValueError,np.linalg.LinAlgError):return [],'위치·카메라 기준 확인 필요'


def all_anchors(frame,results,adopted,catalog,profile,reference,placement,endpoint='명령 위치'):
    anchors=[];messages={}
    for station in ('운반용 지그','리니어 조립','완성품 팔레트'):
        rows,message=anchors_for(frame,results,adopted,catalog,profile,reference,station,'전체',placement,endpoint)
        if station=='리니어 조립' and len(rows)>2 and not any(r['inspection_allowed'] for r in rows):
            rows=rows[:2]
            for row in rows:row['pending_position']=True
            message='리니어 명령 위치 미확인 · 전후진 목표 선택 가능'
        for row in rows:row['station']=station
        anchors.extend(rows);messages[station]=message
    return anchors,messages


def draw_linear_centers(image,profile,reference,placement,endpoint='명령 위치'):
    from .inspection_overlay import draw_label
    anchors,_=anchors_for(image,{},{},{},profile,reference,'리니어 조립','전체',placement,endpoint)
    if not any(a['inspection_allowed'] for a in anchors):return []
    marks=[]
    for row in anchors:
        try:
            uv=project([[0,0,4.25]],row,profile)[0];x,y=np.rint(uv).astype(int)
            if not (0<=x<image.shape[1] and 0<=y<image.shape[0]):continue
            cv2.circle(image,(x,y),7,(20,25,30),-1,cv2.LINE_AA)
            cv2.circle(image,(x,y),4,(255,230,30),-1,cv2.LINE_AA)
            label=row['label']+' 중심'
            if len(anchors)>2:label+=' · '+row['source'].split(' · ')[-1]
            draw_label(image,label,(x+10,y+8),below=True)
            marks.append({'id':row['id'],'point':[float(uv[0]),float(uv[1])],'source':row['source']})
        except (ValueError,KeyError,cv2.error):continue
    return marks
