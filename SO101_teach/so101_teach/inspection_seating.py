"""Image-plane containment and station-specific visual acceptance."""
import cv2
import numpy as np

COLORS={'normal':(60,180,65),'abnormal':(45,45,225),'outside':(20,215,245),'waiting':(165,165,165)}


def containment(observed,roi,*,boundary_fraction=.002,details=None):
    """Ignore isolated color noise; classify actual body versus the fixed ROI."""
    body=(observed>0).astype(np.uint8);allowed=roi>0
    size=int(body.sum())
    if details is not None:details.update(body_pixels=size,boundary_tolerance_percent=100*boundary_fraction)
    if size<20:return 'missing'
    overlap=int(np.count_nonzero(body & allowed))
    if details is not None:details['outside_percent']=round(100*(size-overlap)/size,2)
    if overlap==0:return 'outside'
    interior=cv2.erode(allowed.astype(np.uint8),np.ones((3,3),np.uint8))>0
    touching=int(np.count_nonzero((body>0)&~interior))
    if details is not None:details['boundary_percent']=round(100*touching/size,2)
    return 'boundary' if touching>=max(4,round(size*boundary_fraction)) else 'inside'


def apply_quality(row,anchor,seating=None):
    state=row['state'];row['seating']=seating
    if state=='checking':row['quality']='waiting';return row
    if seating=='outside':row.update(quality='outside',label='ROI 밖');return row
    if state=='cap_only':
        row.update(quality='abnormal',label='상단 단품 · 미완성',reason='상단 단품 높이·형상 일치 · 완성품 아님 · '+row['reason']);return row
    if state=='wrong_part':
        row.update(quality='abnormal',label='다른 부품'+(' · 오안착' if seating=='boundary' else ''));return row
    if anchor.get('part')=='finished' and state in ('housing_seated','insert_added'):
        row.update(quality='abnormal',label='미완성 · '+row['label'],reason='상단 결합 필요'+(' · ROI 경계 걸침' if seating=='boundary' else '')+' · '+row['reason']);return row
    if seating=='boundary':row.update(quality='abnormal',label='부정확한 안착',reason='부품이 ROI 경계에 걸침 · '+row['reason']);return row
    if state=='unknown':row['quality']='abnormal' if row.get('score') is not None else 'waiting';return row
    if state=='empty':row.update(quality='abnormal',label='부품 없음');return row
    expected=anchor.get('expected_product')
    if expected in ('A','B'):
        row['expected_product']=expected
        if not row.get('product_certain'):
            row.update(quality='abnormal',label='제품 구분 미확인',reason=f'{expected}용 팔레트 · A/B 구분 근거 부족 · '+row['reason']);return row
        if row.get('product')!=expected:
            row.update(quality='abnormal',label='다른 제품 · '+row['label'],reason=f'{expected}용 팔레트에 {row.get("product")} 제품 감지 · '+row['reason']);return row
    if row.get('missing_visible_parts'):
        names={'housing':'하단','insert':'중단','cap':'상단'}
        row.update(quality='abnormal',label='필수 부품 미확인',reason='영상에서 보여야 할 '+', '.join(names[p] for p in row['missing_visible_parts'])+' 근거 부족');return row
    if anchor.get('part')=='finished' and state!='cap_added':
        row.update(quality='abnormal',label='미완성 · '+row['label'],reason='완성품 팔레트는 상단 결합까지 필요 · '+row['reason']);return row
    row['quality']='normal' if seating in ('inside',None) else 'abnormal'
    return row


def assembled_insert_quality(row,hsv,masks,lo,pixel_axes,anchor,profile,reference,top_z):
    """Check the exposed insert itself, not just the surrounding housing."""
    delta=pixel_axes@row['offset_mm']
    expected=cv2.warpAffine(masks['insert'],np.float32([[1,0,delta[0]],[0,1,delta[1]]]),(hsv.shape[1],hsv.shape[0]))
    difference=np.abs(hsv[:,:,0].astype(float)-reference['hue']);difference=np.minimum(difference,180-difference)
    color=((difference<reference['tolerance'])&(hsv[:,:,1]>48)&(hsv[:,:,2]>30)).astype(np.uint8)*255
    color=cv2.morphologyEx(color,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    count,labels,stats,_=cv2.connectedComponentsWithStats(color)
    choices=[(np.count_nonzero((labels==i)&(expected>0)),i) for i in range(1,count) if stats[i,cv2.CC_STAT_AREA]>=20]
    if not choices or max(choices)[0]<20:
        row.update(quality='abnormal',label='중단 확인 부족',reason='중단 외곽을 확인할 수 없음 · '+row['reason']);return
    body=(labels==max(choices)[1]).astype(np.uint8)*255
    sizes=[];centers=[]
    for mask in (body,expected):
        contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        if not contours:return
        polygon=cv2.convexHull(np.concatenate(contours))[:,0,:]+lo
        rays=cv2.undistortPoints(polygon.astype(float)[:,None,:],np.asarray(profile['intrinsics']['K'],float),np.asarray(profile['intrinsics']['D'],float)).reshape(-1,2)
        transform=np.linalg.inv(anchor['camera_from_local']);direction=np.c_[rays,np.ones(len(rays))]@transform[:3,:3].T
        xy=transform[:3,3]+direction*((top_z-transform[2,3])/direction[:,2])[:,None]
        _,size,_=cv2.minAreaRect(np.float32(xy[:,:2]));sizes.append(size)
        moment=cv2.moments(mask);centers.append(np.array([moment['m10'],moment['m01']])/moment['m00'])
    if any(min(size)<1 for size in sizes):
        row.update(quality='abnormal',label='중단 확인 부족',reason='중단 외곽 크기 부족 · '+row['reason']);return
    ratios=[max(size)/min(size) for size in sizes];deformation=max(ratios[0]/ratios[1],ratios[1]/ratios[0])
    offset=np.linalg.solve(pixel_axes,centers[0]-centers[1]);distance=float(np.linalg.norm(offset))
    row.update(insert_shape_ratio=round(deformation,3),insert_shape_tolerance=1.20,insert_center_error_mm=round(distance,2),insert_center_tolerance_mm=3.)
    if deformation>1.20:
        row.update(quality='abnormal',label='중단 삽입 불량 의심',reason=f'중단 외형 변형 비율 {deformation:.2f} (허용 1.20) · 기울어진 삽입 또는 가림 · '+row['reason'])
    if distance>3:
        row.update(quality='abnormal',label='중단 삽입 위치 이탈',reason=f'중단 중심 편차 {distance:.1f} mm (허용 3 mm) · '+row['reason'])
