"""Read-only CAD top-shape comparison, independent of robot execution decisions.
Scores are similarity indices, never calibrated probabilities or mechanical PASS.
"""
from copy import deepcopy
from pathlib import Path
import json
import queue
import threading
import time
import cv2
import numpy as np
from .domain import ROOT
from .inspection_geometry import project
from .inspection_seating import containment,apply_quality,assembled_insert_quality

STATES={'cap_visible':'상단 위치·형상 확인 · 높이 미확인','cap_only':'상단 단품 · 미완성','empty':'부품 없음','present':'형상 일치','housing_seated':'하단 안착 형상','insert_added':'중단 삽입 형상','cap_added':'상단 결합 형상'}
PARTS={'housing':'하단','insert':'중단','cap':'상단'}
INSERT_POSITION_TOLERANCE_MM=3.
A_BALL_POSITION_TOLERANCE_MM=5.
B_INSERT_OUTLINE_TOLERANCE_MM=6.
CARRIER_HOUSING_MARGIN_MM=2.
CARRIER_HOUSING_POSITION_TOLERANCE_MM=5.
BOUNDARY_FRACTION=.01
ROTATION_TOLERANCE_DEG=12.


class ShapeInspector:
    def __init__(self,directory=None):
        directory=Path(directory or ROOT/'inspection')
        self.meta=json.loads((directory/'shape_templates.json').read_text())['templates']
        self.appearance=json.loads((directory/'appearance_reference.json').read_text())['colors']
        with np.load(directory/'shape_templates.npz',allow_pickle=False) as source:self.arrays={k:source[k] for k in source.files}
        self.cache={}

    def projected(self,anchor,profile,shape):
        key=(tuple(anchor['templates']),tuple(np.round(anchor['camera_from_local'].ravel(),3)),json.dumps(profile['intrinsics'],sort_keys=True),tuple(shape[:2]))
        if key in self.cache:return self.cache[key]
        projected={name:{feature:project(self.arrays[name+'__'+feature],anchor,profile) if len(self.arrays[name+'__'+feature]) else np.empty((0,2)) for feature in ('edge','color','housing','insert','cap')} for name in anchor['templates']}
        bounds=np.concatenate([points for masks in projected.values() for points in masks.values()])
        lo=np.floor(bounds.min(0)-35).astype(int);hi=np.ceil(bounds.max(0)+35).astype(int)
        h,w=shape[:2]
        lo=np.maximum(lo,0);hi=np.minimum(hi,[w-1,h-1])
        size=(hi-lo)[::-1]+1
        if min(size)<30:raise ValueError('부품 영상이 너무 작음')
        templates=[]
        for name in anchor['templates']:
            masks={}
            for feature in ('edge','color','housing','insert','cap'):
                points=projected[name][feature];mask=np.zeros(size,np.uint8)
                if len(points):
                    uv=np.rint(points-lo).astype(int)
                    valid=(uv[:,0]>=0)&(uv[:,0]<size[1])&(uv[:,1]>=0)&(uv[:,1]<size[0]);uv=uv[valid]
                    masks[feature+'_coverage']=float(valid.mean())
                    mask[uv[:,1],uv[:,0]]=255
                    mask=cv2.morphologyEx(mask,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
                masks[feature]=mask
            expected='carrier_'+anchor['id']+'_present' if anchor.get('part') in PARTS else name
            bound=self.meta[expected].get('bounds_mm')
            if bound is None:
                bound=self.meta[name.replace('_empty','_housing_seated')].get('bounds_mm') if '_empty' in name and not name.startswith('carrier') else [[-25,-25,0],[25,25,25]]
            low,high=np.asarray(bound,float)
            # Use the actual registered fixture boundary (68 / 70 mm), anchored
            # to its accepted pose, with a height envelope for camera parallax.
            half=35 if anchor.get('part')=='finished' else 34
            if anchor.get('part')=='housing':half+=CARRIER_HOUSING_MARGIN_MM
            low[:2]=[-half,-half];high[:2]=[half,half]
            corners=project([[x,y,z] for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])],anchor,profile)
            polygon=cv2.convexHull(np.rint(corners-lo).astype(np.int32))
            masks['roi']=np.zeros(size,np.uint8);cv2.fillConvexPoly(masks['roi'],polygon,255)
            masks['roi_polygon']=(polygon[:,0,:]+lo).tolist()
            meta=self.meta[name]
            face_kind=meta.get('part') or ('cap' if meta['state'] in ('cap_added','cap_only') else 'housing')
            face=self.arrays[name+'__'+face_kind]
            face_z=float(face[:,2].max()) if len(face) else float(self.arrays[name+'__edge'][:,2].max())
            masks['face_z']=face_z
            masks['roi_display_polygon']=project([[-half,-half,face_z],[half,-half,face_z],[half,half,face_z],[-half,half,face_z]],anchor,profile).tolist()
            if anchor.get('part')=='insert':
                # A sphere/cylinder needs its own CAD silhouette, not the square
                # pallet footprint. Keep this region fixed to the slot pose;
                # fitting the observed part must never move the allowed region.
                cloud=self.arrays[expected+'__body']
                body_polygon=cv2.convexHull(project(cloud,anchor,profile).astype(np.float32))
                center=cv2.moments(body_polygon)
                masks['body_center']=np.array([center['m10'],center['m01']])/center['m00']
                angles=np.linspace(0,2*np.pi,16,endpoint=False)
                outline_tolerance=A_BALL_POSITION_TOLERANCE_MM if anchor.get('product')=='A' else B_INSERT_OUTLINE_TOLERANCE_MM
                shifts=np.c_[np.cos(angles),np.sin(angles),np.zeros(16)]*outline_tolerance
                outline=project((cloud[None,:,:]+shifts[:,None,:]).reshape(-1,3),anchor,profile)
                polygon=cv2.convexHull(np.rint(outline-lo).astype(np.int32))
                masks['roi'][:]=0;cv2.fillConvexPoly(masks['roi'],polygon,255)
                masks['roi_polygon']=(polygon[:,0,:]+lo).tolist()
                masks['roi_display_polygon']=masks['roi_polygon']
            if len(face):
                minimum=face[:,:2].min(0);maximum=face[:,:2].max(0)
                masks['face_corners']=np.array([[minimum[0],minimum[1]],[maximum[0],minimum[1]],[maximum[0],maximum[1]],[minimum[0],maximum[1]]])
            templates.append((name,masks))
        value=(lo,hi,templates)
        if len(self.cache)>24:self.cache.clear()
        self.cache[key]=value
        return value

    def inspect(self,frame,anchors,profile):
        rows=[]
        for anchor in anchors:
            if getattr(self,'stop',None) is not None and self.stop.is_set():break
            row={'id':anchor['id'],'state':'unknown','label':'검사 불가','score':None,'reason':'형상 검사 기준 미충족','hidden':[]}
            if 'origin' in anchor:row['target_position_mm']=np.asarray(anchor['origin'],float).round(2).tolist()
            if not anchor['inspection_allowed']:
                row.update(label='목표 위치 표시',reason='현재 명령 수신 후 형상 비교');rows.append(row);continue
            try:
                lo,hi,templates=self.projected(anchor,profile,frame.shape)
                patch=frame[lo[1]:hi[1]+1,lo[0]:hi[0]+1]
                gray=cv2.cvtColor(patch,cv2.COLOR_BGR2GRAY)
                if float(gray.std())<5:raise ValueError('가림·대비 부족')
                hsv=cv2.cvtColor(patch,cv2.COLOR_BGR2HSV)
                color=((hsv[:,:,1]>48)&(hsv[:,:,2]>30)).astype(np.uint8)*255
                color=cv2.morphologyEx(color,cv2.MORPH_OPEN,np.ones((3,3),np.uint8))
                edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),35,100)
                # Assembly and finished fixtures also compare same-colored
                # housings/caps; retain the cavity evidence at those stations.
                detail_edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),8,24) if anchor.get('part') in ('housing','cap','assembly','finished') else None
                observed_distance=cv2.distanceTransform(255-edges,cv2.DIST_L2,3)
                # Rail/background edges outside the actual fixture do not describe
                # the candidate. Score only the projected fixture footprint.
                region=np.zeros(gray.shape,np.uint8)
                points=np.concatenate([np.column_stack(np.where(m['edge']>0)[::-1]) for _,m in templates])
                if len(points)>2:cv2.fillConvexPoly(region,cv2.convexHull(points.astype(np.int32)),255)
                region=cv2.dilate(region,np.ones((5,5),np.uint8))>0
                # Search a bounded physical neighborhood, retaining the measured
                # image offset as evidence instead of silently moving the anchor.
                axes=project([[0,0,20],[1,0,20],[0,1,20]],anchor,profile)
                pixel_axes=(axes[1:]-axes[0]).T
                radius=min(24,max(3,int(np.ceil(np.linalg.norm(pixel_axes,axis=0).max()*8))))
                def correlate(observed,template):
                    return cv2.matchTemplate(cv2.copyMakeBorder(observed.astype(np.float32),radius,radius,radius,radius,cv2.BORDER_CONSTANT),template.astype(np.float32),cv2.TM_CCORR)
                observed_edges=(edges>0).astype(np.float32);observed_color=(color>0).astype(np.float32)
                edge_count=correlate(observed_edges,region);color_count=correlate(observed_color,region)
                # White empty pallets have shallow edges on a white table.
                # Use softer edges only for their empty-state templates.
                empty_edges=cv2.Canny(cv2.GaussianBlur(gray,(3,3),0),15,45) if anchor.get('part')=='finished' else None
                empty_distance=cv2.distanceTransform(255-empty_edges,cv2.DIST_L2,3) if empty_edges is not None else None
                empty_count=correlate(empty_edges>0,region) if empty_edges is not None else None
                yy,xx=np.mgrid[-radius:radius+1,-radius:radius+1]
                shift_penalty=np.hypot(xx,yy)*.001
                local_offsets=np.linalg.solve(pixel_axes,np.stack([xx,yy]).reshape(2,-1)).reshape(2,*xx.shape)
                bounded=np.linalg.norm(local_offsets,axis=0)<=8
                results=[]
                for name,masks in templates:
                    if getattr(self,'stop',None) is not None and self.stop.is_set():break
                    if masks.get('color_coverage',1)<.98 or masks.get('edge_coverage',1)<.85:continue
                    predicted=masks['edge'];expected=masks['color']>0
                    distances=cv2.distanceTransform(255-predicted,cv2.DIST_L2,3)
                    empty_fixture=empty_edges is not None and self.meta[name]['state']=='empty'
                    image_distance=empty_distance if empty_fixture else observed_distance
                    image_edges=(empty_edges>0).astype(np.float32) if empty_fixture else observed_edges
                    image_count=empty_count if empty_fixture else edge_count
                    recall=correlate(np.exp(-image_distance/2),predicted>0)/max(1,int((predicted>0).sum()))
                    precision=correlate(image_edges,np.exp(-distances/2)*region)/np.maximum(1,image_count)
                    shape_score=2*recall*precision/np.maximum(.001,recall+precision)
                    n=int(expected.sum())
                    if n:
                        intersection=correlate(observed_color,expected)
                        silhouette=intersection/np.maximum(1,n+color_count-intersection)
                        score=.4*silhouette+.6*shape_score
                    else:
                        # Only a matching empty fixture supports an absence observation.
                        silhouette=np.maximum(0,1-color_count/max(1,int(region.sum()))*12)
                        score=shape_score*silhouette
                    covered=correlate(np.ones(gray.shape,np.float32),expected if n else predicted>0)/max(1,n if n else int((predicted>0).sum()))
                    allowed=bounded & (covered>=(.98 if n else .85))
                    if not allowed.any():continue
                    y,x=np.unravel_index(np.argmax(np.where(allowed,score-shift_penalty,-1)),score.shape)
                    offset=np.linalg.solve(pixel_axes,[x-radius,y-radius])
                    material=None;material_parts={}
                    if n:
                        # Evaluate each CAD-exposed material separately. A filled
                        # cavity has the same outer silhouette as an empty housing;
                        # a coherent, differently colored central insert is evidence.
                        shifted=cv2.warpAffine(hsv,np.float32([[1,0,radius-x],[0,1,radius-y]]),(hsv.shape[1],hsv.shape[0]),flags=cv2.INTER_NEAREST)
                        coherence=[];hues=[]
                        for part in ('housing','insert','cap'):
                            mask=cv2.erode(masks[part],np.ones((5,5),np.uint8))>0
                            if mask.sum()<20:continue
                            pixels=shifted[mask];active=(pixels[:,1]>48)&(pixels[:,2]>30)
                            histogram=np.bincount((pixels[active,0]//5).astype(int),minlength=36).astype(float)
                            smooth=histogram+np.roll(histogram,1)+np.roll(histogram,-1)
                            coherent=float(smooth.max()/max(1,len(pixels)))
                            reference=self.appearance.get(self.meta[name]['product']+'_'+part)
                            if reference and active.any():
                                delta=np.abs(pixels[active,0].astype(float)-reference['hue']);delta=np.minimum(delta,180-delta)
                                coherent*=float(np.exp(-.5*(delta/reference['tolerance'])**2).mean())
                            coherence.append(coherent)
                            material_parts[part]=coherent
                            hues.append(int(smooth.argmax()))
                        if coherence:
                            material=float(np.mean(coherence))
                            if len(hues)>1 and min(min(abs(a-b),36-abs(a-b)) for i,a in enumerate(hues) for b in hues[i+1:])<3:material*=.65
                    combined=float(score[y,x]) if material is None else .55*float(score[y,x])+.45*material
                    detail=None
                    if detail_edges is not None and n:
                        # Same-colored caps and housings differ mainly at the
                        # shallow cavity. Examine low-contrast edges only inside
                        # the body, where jig/floor edges cannot supply evidence.
                        interior=np.zeros(gray.shape,np.uint8)
                        points=np.column_stack(np.where(expected)[::-1]).astype(np.int32)
                        cv2.fillConvexPoly(interior,cv2.convexHull(points),255)
                        inset=max(3,round(float(np.linalg.norm(pixel_axes,axis=0).min())*7))
                        interior=cv2.erode(interior,np.ones((2*inset+1,2*inset+1),np.uint8))>0
                        if interior.sum()>=100:
                            observed=cv2.warpAffine(detail_edges,np.float32([[1,0,radius-x],[0,1,radius-y]]),(gray.shape[1],gray.shape[0]))
                            actual=(observed>0)&interior;expected_detail=(predicted>0)&interior
                            if expected_detail.sum()>=20:
                                distance=cv2.distanceTransform(255-observed,cv2.DIST_L2,3)
                                recall_detail=float(np.exp(-distance[expected_detail]/2).mean())
                                precision_detail=float(np.exp(-distances[actual]/2).mean()) if actual.any() else 0.
                                detail=2*recall_detail*precision_detail/max(.001,recall_detail+precision_detail)
                            else:detail=max(0.,1-float(actual.sum())/max(1,float(interior.sum())*.04))
                    results.append({'template':name,'score':combined,'shape':float(shape_score[y,x]),'silhouette':float(silhouette[y,x]),'material':material,'material_parts':material_parts,'interior_detail':detail,'offset_mm':offset.round(2).tolist()})
                # Competing templates belong to a known fixture. A template
                # needing a large translation is weaker geometric evidence.
                for candidate in results:candidate['rank_score']=candidate['score']-.008*float(np.linalg.norm(candidate['offset_mm']))+.10*(candidate['interior_detail'] or 0.)
                results.sort(key=lambda r:r['rank_score'],reverse=True)
                if not results:raise ValueError('화면 밖·잘린 부품 영역')
                best=results[0];meta=self.meta[best['template']]
                ambiguous_b_top=anchor.get('part')=='assembly' and meta['product']=='B' and meta['state'] in ('cap_only','cap_added')
                def visual_state(value):
                    if ambiguous_b_top and value['product']=='B' and value['state'] in ('cap_only','cap_added'):return 'cap_visible'
                    return value['state']
                observed_state=visual_state(meta)
                # Identical empty fixtures/A-B external surfaces are one visual state.
                rival=next((r for r in results[1:] if visual_state(self.meta[r['template']])!=observed_state),None)
                margin=best['rank_score']-(rival['rank_score'] if rival else 0)
                empty_fixture=anchor.get('part')=='finished' and meta['state']=='empty'
                score_floor,shape_floor,margin_floor=(.45,.45,.10) if empty_fixture else (.50,.32,.02)
                confident=best['score']>=score_floor and margin>=margin_floor and best['shape']>=shape_floor
                if empty_fixture:confident=confident and best['silhouette']>=.98
                row.update(score=round(best['score']*100),template=best['template'],candidates=results,margin=round(margin,3),offset_mm=best['offset_mm'])
                if confident:
                    same_state=[r for r in results if (self.meta[r['template']]['state'] in ('cap_only','cap_added') if observed_state=='cap_visible' else self.meta[r['template']]['state']==meta['state']) and best['score']-r['score']<.08]
                    groups={self.meta[r['template']]['product'] for r in same_state}
                    product=meta['product']+' ' if len(groups)==1 else ''
                    label=STATES.get(observed_state,'다른 부품')
                    if meta['state'] not in ('empty','present'):label=product+label
                    hidden=meta.get('hidden',[])
                    row.update(state=observed_state,label=label,product=meta['product'],product_certain=len(groups)==1,hidden=hidden,
                               reason=('내부 '+', '.join(PARTS[p] for p in hidden)+' 가림 · 미확인') if hidden else '윗면 외곽·홈 비교')
                    if observed_state=='cap_visible':
                        row.update(hidden=['housing','insert'],height_verified=False,reason='B 상단 위치·형상 확인 · 윗면 영상만으로 단품·완성품 높이 구분 불가')
                    if meta['state']=='wrong_part':row['reason']='감지: '+meta['product']+' '+PARTS[meta['part']]+' · 지정 부품과 다름'
                    if anchor.get('part')!='insert' and np.linalg.norm(best['offset_mm'])>2:
                        dx,dy=best['offset_mm'];row['reason']+=f' · 위치차 {dx:+.1f}/{dy:+.1f} mm'
                else:
                    row['reason']=('부품 구분 기준 미충족' if margin<margin_floor else '색상·외곽·홈 검사 기준 미충족')+f' · 일치 {best["score"]*100:.0f}/{score_floor*100:g}점 · 외곽 {best["shape"]:.2f}/{shape_floor:g} · 상태차 {margin:.2f}/{margin_floor:g}'
                masks=dict(templates)[best['template']];roi=masks['roi'];row['roi_polygon']=masks['roi_polygon'];row['roi_display_polygon']=masks['roi_display_polygon']
                row['missing_visible_parts']=[p for p in meta.get('visible',[]) if best['material_parts'].get(p,0)<.3]
                delta=pixel_axes@best['offset_mm'];target=cv2.warpAffine(masks['color'],np.float32([[1,0,delta[0]],[0,1,delta[1]]]),(gray.shape[1],gray.shape[0]))>0
                # Low-saturation floor/shadow noise must not become part of a
                # saturated component simply because it touches a jig edge.
                sample=hsv[target & (color>0)]
                saturation=max(48,float(np.median(sample[:,1]))*.45) if len(sample) else 48
                body_color=color.copy();body_color[hsv[:,:,1]<saturation]=0
                kinds=list(best['material_parts']) or ([meta['part']] if meta.get('part') else [])
                references=[self.appearance[meta['product']+'_'+p] for p in kinds if meta['product']+'_'+p in self.appearance]
                if references:
                    hue_ok=np.zeros(gray.shape,bool)
                    for reference in references:
                        delta=np.abs(hsv[:,:,0].astype(float)-reference['hue']);delta=np.minimum(delta,180-delta)
                        hue_ok |= delta<=reference['tolerance']*1.5
                    body_color[~hue_ok]=0
                count,labels,stats,centers=cv2.connectedComponentsWithStats(cv2.morphologyEx(body_color,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8)))
                body=np.zeros(gray.shape,np.uint8)
                for i in range(1,count):
                    component=labels==i;overlap=np.count_nonzero(component&target)
                    if stats[i,cv2.CC_STAT_AREA]>=30 and overlap>=max(12,stats[i,cv2.CC_STAT_AREA]*.15):body[component]=255
                if not np.any(body) and meta['state']!='empty':
                    center=np.mean(np.asarray(masks['roi_polygon']),axis=0)-lo
                    options=[i for i in range(1,count) if 30<stats[i,cv2.CC_STAT_AREA]<max(100,target.sum()*2) and np.linalg.norm(centers[i]-center)<70]
                    if options:body[labels==min(options,key=lambda i:np.linalg.norm(centers[i]-center))]=255
                if not np.any(body):
                    # A visible nearby part entirely outside this ROI is yellow.
                    # Do not steal a component belonging to another fixture.
                    center=np.mean(np.asarray(masks['roi_polygon']),axis=0)-lo
                    occupied=next((m for name,m in templates if self.meta[name]['state'] in ('present','housing_seated')),None)
                    expected_size=int(np.count_nonzero(occupied['color'])) if occupied else 0
                    options=[]
                    for i in range(1,count):
                        size=stats[i,cv2.CC_STAT_AREA];component=labels==i
                        if not .35*expected_size<size<1.8*max(expected_size,1) or np.any(component & (roi>0)):continue
                        distance=np.linalg.norm(centers[i]-center)
                        if distance>max(roi.shape)*.65:continue
                        keys=[anchor.get('product','')+'_'+anchor.get('part','')]
                        references=[self.appearance[k] for k in keys if k in self.appearance]
                        if references:
                            hue=hsv[component,0].astype(float);delta=np.abs(hue-references[0]['hue']);delta=np.minimum(delta,180-delta)
                            if np.mean(delta<references[0]['tolerance'])<.65:continue
                        other_centers=[project([[0,0,20]],a,profile)[0]-lo for a in anchors if a['id']!=anchor['id']]
                        if any(np.linalg.norm(centers[i]-point)<distance for point in other_centers):continue
                        options.append((distance,i))
                    if options:body[labels==min(options)[1]]=255
                # All stations allow the same small segmentation fringe.
                seating=containment(body,roi,boundary_fraction=BOUNDARY_FRACTION,details=row)
                row['observed_polygon']=[]
                contours,_=cv2.findContours(body,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                if contours:row['observed_polygon']=(cv2.convexHull(np.concatenate(contours))[:,0,:]+lo).tolist()
                apply_quality(row,anchor,seating)
                if row['state']=='insert_added' and anchor.get('part') in ('assembly','finished') and meta['product']+'_insert' in self.appearance:
                    insert_points=self.arrays[best['template']+'__insert']
                    if len(insert_points):assembled_insert_quality(row,hsv,masks,lo,pixel_axes,anchor,profile,self.appearance[meta['product']+'_insert'],float(insert_points[:,2].max()))
                if anchor.get('part')=='insert' and row['state']=='present' and contours and seating!='outside':
                    # Top-only template alignment includes jig edges. Seating
                    # instead compares the complete CAD body with the isolated
                    # colored body, including the visible side of a tall insert.
                    center=cv2.moments(np.asarray(row['observed_polygon'],np.float32))
                    observed_center=np.array([center['m10'],center['m01']])/center['m00']
                    center_offset=np.linalg.solve(pixel_axes,observed_center-masks['body_center'])
                    distance=float(np.linalg.norm(center_offset))
                    row['center_offset_mm']=center_offset.round(2).tolist()
                    row['center_error_mm']=round(distance,1)
                    tolerance=A_BALL_POSITION_TOLERANCE_MM if anchor.get('product')=='A' else INSERT_POSITION_TOLERANCE_MM
                    row['center_tolerance_mm']=tolerance
                    if distance>tolerance:
                        row.update(quality='abnormal',label='부정확한 안착',reason=f'슬롯 중심 이탈 {distance:.1f} mm (허용 {tolerance:g} mm) · '+row['reason'])
                if anchor.get('part')=='housing' and row['state']=='present' and seating!='outside':
                    # The height envelope tolerates perspective and edge noise;
                    # a separate fixture-relative translation limit prevents it
                    # from accepting a genuinely shifted housing.
                    distance=float(np.linalg.norm(best['offset_mm']))
                    row.update(center_offset_mm=best['offset_mm'],center_error_mm=round(distance,1),center_tolerance_mm=CARRIER_HOUSING_POSITION_TOLERANCE_MM)
                    if distance>CARRIER_HOUSING_POSITION_TOLERANCE_MM:
                        row.update(quality='abnormal',label='부정확한 안착',reason=f'슬롯 중심 이탈 {distance:.1f} mm (허용 {CARRIER_HOUSING_POSITION_TOLERANCE_MM:g} mm) · '+row['reason'])
                face_angle=0.
                if contours and row['state'] not in ('empty','unknown','wrong_part') and anchor.get('part')!='insert':
                    # Square housings/caps must follow the fixture axes. A round
                    # insert alone has no observable yaw and is excluded.
                    polygon=np.asarray(row['observed_polygon'],float)
                    rays=cv2.undistortPoints(polygon[:,None,:],np.asarray(profile['intrinsics']['K'],float),np.asarray(profile['intrinsics']['D'],float)).reshape(-1,2)
                    T=np.linalg.inv(anchor['camera_from_local']);direction=np.c_[rays,np.ones(len(rays))]@T[:3,:3].T
                    z=self.meta[best['template']].get('bounds_mm',[[0,0,0],[0,0,20]])[1][2]
                    xy=T[:3,3]+direction*((z-T[2,3])/direction[:,2])[:,None]
                    _,size,angle=cv2.minAreaRect(np.float32(xy[:,:2]))
                    error=min(angle%90,90-angle%90);row['rotation_error_deg']=round(error,1);row['rotation_tolerance_deg']=ROTATION_TOLERANCE_DEG
                    face_angle=angle%90 if angle%90<=45 else angle%90-90
                    if min(size)>20 and max(size)/min(size)<1.6 and error>ROTATION_TOLERANCE_DEG:
                        row.update(quality='abnormal',label='부정확한 안착',reason=f'지그 축과 방향 차이 {error:.1f}° (허용 {ROTATION_TOLERANCE_DEG:g}°) · '+row['reason'])
                # Render a single face, never the convex hull of multiple heights.
                # The full height envelope remains internal to containment checks.
                if 'face_corners' in masks and contours and anchor.get('part')!='insert':
                    a=np.deg2rad(face_angle);rotation=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]])
                    corners=masks['face_corners']@rotation.T+best['offset_mm']
                    row['part_top_polygon']=project(np.c_[corners,np.full(4,masks['face_z'])],anchor,profile).tolist()
            except (ValueError,KeyError,cv2.error,np.linalg.LinAlgError) as exc:row['reason']=str(exc)
            row.setdefault('quality','waiting')
            rows.append(row)
        return rows


class InspectionWorker(threading.Thread):
    """One latest-only job; UI never waits for vision. Old selection results expire."""
    def __init__(self):
        super().__init__(daemon=True,name='part-shape-inspector');self.jobs=queue.Queue(maxsize=1);self.stop=threading.Event();self.result=None;self.start()
    def submit(self,key,at,frame,anchors,profile,*,recovery=None):
        job=(key,at,frame.copy(),deepcopy(anchors),deepcopy(profile),deepcopy(recovery))
        try:self.jobs.get_nowait()
        except queue.Empty:pass
        self.jobs.put_nowait(job)
    def close(self):
        self.stop.set()
        if self.is_alive() and threading.current_thread() is not self:self.join(timeout=1)
    def run(self):
        inspector=None;error=None
        try:inspector=ShapeInspector();inspector.stop=self.stop
        except Exception as exc:error=str(exc)
        previous={};last_key=None;last_at=None;last_completed=None;tracker=None;tracker_path=None
        while not self.stop.is_set():
            try:key,at,frame,anchors,profile,recovery=self.jobs.get(timeout=.1)
            except queue.Empty:continue
            if inspector:error=None
            try:
                if recovery:
                    from .occupied_pallet import locate
                    from .inspection_geometry import anchors_for
                    jid=recovery['reference']['stations']['finished_pallet']['jig_id'];config=recovery['catalog'].get(jid)
                    directory=recovery.get('reference_directory');reference_file=Path(directory)/'pallet.json' if directory else None
                    signature=(str(reference_file),reference_file.stat().st_mtime_ns) if reference_file and reference_file.exists() else None
                    if signature!=tracker_path:
                        tracker=None;tracker_path=signature
                        if signature:
                            from .inspection_tracking import PalletReference
                            try:tracker=PalletReference(directory)
                            except (OSError,ValueError,KeyError):tracker=None
                    result=tracker.locate(frame,profile,config) if tracker and config else None
                    if not result and not any(a['part']=='finished' for a in anchors):
                        result=locate(frame,config,profile) if config else None
                        if result and tracker:result['inspection_source']='현재 외곽 재탐색 · 기준 영상 추적 범위 밖'
                    if result:
                        extra,_=anchors_for(frame,{jid:result},{},recovery['catalog'],profile,recovery['reference'],'완성품 팔레트','전체',recovery['placement'])
                        for a in extra:a['station']='완성품 팔레트'
                        anchors=[a for a in anchors if a['part']!='finished']+extra
                rows=inspector.inspect(frame,anchors,profile) if inspector else []
            except Exception as exc:rows=[];error=str(exc)
            # A slow full-scene pass must not reset confirmation on every job.
            # Allow its own processing interval plus the normal one-second gap.
            gap=max(1.,last_completed-last_at+1.) if last_at is not None and last_completed is not None else 1.
            if key!=last_key or last_at is None or at-last_at>min(5.,gap):previous={}
            current={}
            for row in rows:
                state=row['state'];signature=(state,row.get('quality'),row.get('product'));old,count=previous.get(row['id'],(None,0))
                count=count+1 if old==signature and at!=last_at else 1
                current[row['id']]=(signature,count)
                if state not in ('unknown',) and count<2:row.update(state='checking',quality='waiting',label='확인 중',reason='다음 새 영상에서 같은 형상 확인')
            previous=current;last_key=key;last_at=at;last_completed=time.monotonic()
            self.result={'key':key,'at':at,'completed':time.monotonic(),'rows':rows,'anchors':anchors,'error':error}
