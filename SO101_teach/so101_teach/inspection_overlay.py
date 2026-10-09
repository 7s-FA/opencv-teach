"""Display CAD search regions on fresh raw camera frames; no quality decisions."""
from functools import lru_cache
import cv2
import numpy as np
from .height_reference import support_bottom_z
from .vision import project_plane
from .jig_heading import mesh_yaw_offset


PART_NAMES={'housing':'하단','insert':'중단','cap':'상단','finished':'완성품'}


@lru_cache(maxsize=32)
def label_image(text):
    from PIL import Image, ImageDraw
    from .vision import roi_font
    font=roi_font()
    if font is not None:font=font.font_variant(size=20)
    image=Image.new('RGB',(1,1));draw=ImageDraw.Draw(image)
    box=draw.textbbox((0,0),text,font=font)
    image=Image.new('RGB',(box[2]-box[0]+12,box[3]-box[1]+10),'#132b3a')
    ImageDraw.Draw(image).text((6-box[0],5-box[1]),text,font=font,fill='white')
    return cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)


def draw_label(image,text,point,*,below=False,layout=None):
    badge=label_image(text);h,w=badge.shape[:2];ih,iw=image.shape[:2]
    if w>iw or h>ih:return
    if layout is not None:
        position=layout.place(w,h,point)
        if position is None:return
        x,y=position
    else:
        x=max(0,min(iw-w,int(point[0])));y=max(0,min(ih-h,int(point[1])+3 if below else int(point[1])-h-3))
    image[y:y+h,x:x+w]=badge


def detection_summary(results,catalog,*,fresh):
    lines=[]
    for key,config in catalog.items():
        result=results.get(key) or {};selected=result.get('selected')
        if not fresh:state='검출 갱신 대기'
        elif result.get('pose_held') or result.get('pose_frozen') or result.get('teaching_held'):state='이전 위치 유지'
        elif key not in results:state='검출 결과 없음'
        elif selected:state='검출됨' + (' · 방향 확인' if selected.get('orientation_verified') else '')
        else:state={'orientation_unconfirmed':'앞뒤 방향 확인 중','ambiguous':'후보 여러 개','paused':'검출 정지','processing':'검출 중'}.get(result.get('status'),'미검출')
        lines.append(f'{config["name"]} · {state}')
    return '\n\n'.join(lines) or '등록된 지그 없음'


def overlay(frame, results, catalog, profile, reference, station, product, *, report=None):
    output=frame.copy()
    if report is not None:report['projected_parts']=[]
    if station=='리니어 조립':
        return output, '리니어 팔레트의 카메라 감지 기준 미등록 · CAD 설명에서 조립 순서를 확인하세요.'
    key='carrier' if station=='운반용 지그' else 'finished_pallet'
    spec=reference['stations'][key];jid=spec['jig_id']
    result=results.get(jid) or {};item=result.get('selected') or {};metric=item.get('metric')
    if not metric or result.get('pose_held') or result.get('pose_frozen') or result.get('teaching_held'):
        return output, '현재 지그 감지 대기 · 유지된 과거 위치에는 부품 ROI를 표시하지 않습니다.'
    outline=item.get('outline_px',item.get('quad'))
    if outline is not None:
        outline=np.asarray(outline,float)
        if outline.ndim==2 and outline.shape[1]==2 and len(outline)>=3 and np.isfinite(outline).all() and np.abs(outline).max()<100000:
            cv2.polylines(output,[np.rint(outline).astype(np.int32)],True,(180,210,35),2,cv2.LINE_AA)
            draw_label(output,'검출 지그 · '+catalog.get(jid,{}).get('name',station),(outline[:,0].min(),outline[:,1].max()),below=True)
    if key=='carrier' and (not item.get('orientation_verified') or metric.get('symmetry_deg')!=360):
        return output, '운반 지그의 앞뒤 방향 확인 대기 · 기준 구멍이 보여야 합니다.'
    intrinsics=profile.get('intrinsics') or {}
    if list(intrinsics.get('size',[]))!=[frame.shape[1],frame.shape[0]] or not profile.get('extrinsics'):
        return output, '현재 영상 해상도에 맞는 카메라 보정이 필요합니다.'
    calibrated=intrinsics.get('camera',{}).get('source');source=profile.get('camera',{}).get('source')
    if calibrated is not None and source is not None and str(calibrated)!=str(source):
        return output, '현재 카메라와 보정 카메라가 다릅니다.'
    try:
        yaw=np.deg2rad(float(metric['yaw_deg'])+mesh_yaw_offset(metric))
        rotation=np.array([[np.cos(yaw),-np.sin(yaw)],[np.sin(yaw),np.cos(yaw)]])
        center=np.asarray(metric['center_xy_mm'],float);bottom=support_bottom_z(catalog[jid],profile)
        rois=[r for r in spec['rois'] if r['product']==product] if key=='carrier' else [spec['rois'][product]]
        polygons=[]
        for roi in rois:
            x0,y0,x1,y1=roi['search_roi_xy_mm']
            corners=np.array([[x0,y0],[x1,y0],[x1,y1],[x0,y1]])
            # Sample edges so raw lens distortion is represented by curves.
            points=np.concatenate([a+(b-a)*np.linspace(0,1,16,endpoint=False)[:,None] for a,b in zip(corners,np.roll(corners,-1,axis=0))])
            xy=points@rotation.T+center;z=bottom+roi['part_z_range_mm'][1]
            T=np.linalg.inv(np.asarray(profile['extrinsics']['base_from_camera'],float))
            camera=np.c_[xy,np.full(len(xy),z)]@T[:3,:3].T+T[:3,3]
            if not np.isfinite(camera).all() or np.any(camera[:,2]<=0):raise ValueError('투영 깊이')
            uv=project_plane(xy,profile,z)
            if not np.isfinite(uv).all() or np.abs(uv).max()>100000:raise ValueError('투영 범위')
            polygons.append((np.rint(uv).astype(np.int32),roi.get('part','finished')))
        for polygon,label in polygons:
            cv2.polylines(output,[polygon],True,(0,180,255),2,cv2.LINE_AA)
            draw_label(output,f'{product} {PART_NAMES[label]} · 예상 ROI',polygon.min(axis=0))
        if report is not None:report['projected_parts']=[label for _,label in polygons]
        return output, f'{station} · {product} CAD 예상 ROI {len(polygons)}개 · 부품 검출/합격 판정 아님'
    except (KeyError,TypeError,ValueError,np.linalg.LinAlgError,cv2.error):
        return output, '지그 위치 또는 카메라 보정값을 확인하세요. ROI 투영을 보류했습니다.'
