"""Current-frame AI evidence for the existing CAD seating checks.

AI never supplies a robot pose or relaxes the CAD/temporal acceptance criteria.
"""
from copy import deepcopy
import time
import cv2
import numpy as np
from .model_regions import RegionModel,checked_settings
from .inspection_seating import containment,apply_quality

METHODS={'OpenCV':'opencv','AI + OpenCV':'model_roi'}


def inspection_settings(profile):
    # Old profiles retain their applied behavior until a separate choice is saved.
    value=profile.get('part_inspection',profile.get('jig_detection',{}))
    return checked_settings({'jig_detection':value})


def inspection_label(profile):
    settings=inspection_settings(profile)
    return next(label for label,method in METHODS.items() if method==settings['method'])

# Class IDs of the installed, unchanged thirteen-class segmentation model.
PARTS={3:('A','housing','housing_seated'),4:('A','insert','wrong_part'),5:('A','cap','cap_only'),
       6:('B','housing','housing_seated'),7:('B','insert','wrong_part'),8:('B','cap','cap_only'),
       9:('A','assembly','insert_added'),10:('B','assembly','insert_added'),
       11:('A','assembly','cap_added'),12:('B','assembly','cap_added')}
CLASS_LABELS={3:'A 하단',4:'A 중단',5:'A 상단 단품',6:'B 하단',7:'B 중단',8:'B 상단 단품',
              9:'A 하단+중단',10:'B 하단+중단',11:'A 완제품',12:'B 완제품'}


def geometric_fault(row):
    if row.get('seating') in ('boundary','outside'):return True
    return any(row.get(value) is not None and row.get(limit) is not None and row[value]>row[limit]
               for value,limit in (('center_error_mm','center_tolerance_mm'),('rotation_error_deg','rotation_tolerance_deg'),
                                   ('insert_center_error_mm','insert_center_tolerance_mm'),('insert_shape_ratio','insert_shape_tolerance')))


def prefer_ai_label(row,anchor,item,inspector,frame,profile,templates):
    """AI supplies semantics; CAD still has to verify the corresponding geometry."""
    product,state=semantic(item,anchor);original=deepcopy(row)
    equivalent=(state==row['state'] or product=='B' and state=='cap_added' and row['state']=='cap_visible')
    changed=not equivalent or row.get('product_certain') and row.get('product')!=product
    if changed:
        part=PARTS[item['class_id']][1]
        names=[name for name,_ in templates if inspector.meta[name]['product']==product
               and inspector.meta[name]['state']==state
               and (state!='wrong_part' or inspector.meta[name].get('part')==part)]
        if len(names)!=1:
            row.update(state=state,product=product,product_certain=True,quality='waiting',template=None,
                       reason='AI 라벨에 대응하는 CAD 안착 기준 없음')
        else:
            evidence={key:row[key] for key in ('inspection_method','cad_observation','ai_observations')}
            checked=inspector.inspect(frame,[{**anchor,'templates':names}],profile)[0]
            row={**checked,**evidence}
            row['cad_label_recheck']={key:checked.get(key) for key in ('template','state','quality','score')}
            decisive=checked.get('state') not in ('unknown','checking') and checked.get('quality')!='waiting'
            row.update(state=state,product=product,product_certain=True)
            if decisive:apply_quality(row,anchor,row.get('seating'))
            else:row.update(quality='waiting',reason='AI 라벨 확인 · CAD 안착 근거 부족 · '+row.get('reason',''))
            if geometric_fault(checked):
                row.update(quality='outside' if checked.get('seating')=='outside' else 'abnormal',reason=checked['reason'])
    else:
        row.update(product=product,product_certain=True)
        if row.get('label')=='제품 구분 미확인' and row.get('quality')=='waiting':apply_quality(row,anchor,row.get('seating'))
    # A semantic correction must never erase a measured position/angle fault.
    if geometric_fault(original) and not geometric_fault(row):
        row['cad_geometric_fault']={key:original.get(key) for key in ('seating','center_error_mm','center_tolerance_mm','rotation_error_deg','rotation_tolerance_deg','insert_center_error_mm','insert_center_tolerance_mm','insert_shape_ratio','insert_shape_tolerance')}
        row.update(quality='outside' if original.get('seating')=='outside' else 'abnormal',reason=original['reason']+' · '+row.get('reason',''))
        if original.get('seating') in ('boundary','outside'):row['seating']=original['seating']
    if geometric_fault(row):row['quality']='outside' if row.get('seating')=='outside' else 'abnormal'
    row.update(ai_product=product,ai_state=state,ai_label=CLASS_LABELS[item['class_id']],ai_label_preferred=bool(changed))
    return row


def describe_ai_row(row,anchor):
    if row.get('state')=='unknown':return row
    if row['quality']=='waiting':suffix='안착 확인 대기'
    elif row['state']=='wrong_part':suffix='다른 부품'
    elif row['quality'] in ('abnormal','outside') and (geometric_fault(row) or row.get('cad_geometric_fault')):suffix='오안착'
    elif row['state']=='cap_only' or anchor.get('part')=='finished' and row['state']!='cap_added':suffix='미완성'
    elif row.get('missing_visible_parts'):suffix='필수 부품 미확인'
    elif row.get('state')=='cap_visible':suffix='높이 미확인'
    else:suffix='정상' if row['quality']=='normal' else '확인 필요'
    row['label']=row['ai_label']+' · '+suffix
    row['reason']='AI '+row['ai_label']+(' 우선 · ' if row['ai_label_preferred'] else ' 확인 · ')+row.get('reason','')
    return row


def semantic(instance,anchor):
    product,part,state=PARTS[instance['class_id']]
    if anchor.get('part') in ('housing','insert','cap'):
        state='present' if part==anchor['part'] and product==anchor.get('product') else 'wrong_part'
    return product,state


def inconclusive(row,reason):
    # Preserve an existing, observed geometric fault; it must not become a pass.
    keep_fault=row.get('quality') in ('abnormal','outside') and row.get('state')!='empty'
    row['state']='unknown'
    if not keep_fault:row.update(quality='waiting',label='AI 확인 대기')
    row['reason']=reason+' · '+row.get('reason','')
    return row


def combine(rows,anchors,detections,inspector,frame,profile,confidence):
    from .shape_inspection import BOUNDARY_FRACTION
    # Match ShapeInspector's existing V > 30 image-evidence floor. A model
    # can attach a dark background spur to a correctly classified part. Such
    # pixels are not visible body evidence. This is full-frame filtering: it
    # never clips a displaced part to the permitted ROI or moves the ROI.
    visible_body=frame.max(axis=2)>30
    prepared={};by_id={a['id']:a for a in anchors};assigned={a['id']:[] for a in anchors};ambiguous=set()
    for anchor in anchors:
        lo,hi,templates=inspector.projected(anchor,profile,frame.shape)
        region=np.logical_or.reduce([m['roi']>0 for _,m in templates])
        prepared[anchor['id']]=(lo,hi,templates,region)
    objects=[d for d in detections if d['class_id'] in PARTS and d['confidence']>=confidence]
    for item in objects:
        choices=[]
        for key,(lo,hi,templates,region) in prepared.items():
            crop=item['mask'][lo[1]:hi[1]+1,lo[0]:hi[0]+1]
            overlap=int(np.count_nonzero(crop & region))
            if overlap>=20:choices.append((overlap,key))
        if choices:
            best=max(v[0] for v in choices)
            # An equal association cannot silently assign the same part twice.
            targets=[key for overlap,key in choices if overlap==best]
            if len(targets)>1:ambiguous.update(targets)
            else:assigned[targets[0]].append(item)
    output=[]
    for original in rows:
        row=deepcopy(original);key=row['id'];anchor=by_id[key]
        row['inspection_method']='ai_and_cad'
        row['cad_observation']={k:original.get(k) for k in ('state','quality','product','product_certain')}
        lo,hi,templates,region=prepared[key];items=assigned[key]
        row['ai_observations']=[{k:d[k] for k in ('class_id','confidence','box')} for d in items]
        if key in ambiguous:
            output.append(inconclusive(row,'AI 부품의 검사 위치를 하나로 구분할 수 없음'));continue
        if row['state']=='empty' and not items:
            jig_class=1 if anchor.get('part')=='finished' else 2 if anchor.get('part')=='assembly' else 0
            seen=any(d['class_id']==jig_class and d['confidence']>=confidence and
                     np.count_nonzero(d['mask'][lo[1]:hi[1]+1,lo[0]:hi[0]+1] & region)>=20 for d in detections)
            if not seen:row=inconclusive(row,'빈 상태 기준 지그의 현재 AI 관측 없음')
            else:row['reason']='AI 지그 확인·부품 관측 없음 + '+row['reason']
            output.append(row);continue
        if len(items)!=1:
            output.append(inconclusive(row,'AI 부품 미검출' if not items else '검사 영역의 AI 부품 후보가 여러 개'));continue
        item=items[0]
        row=prefer_ai_label(row,anchor,item,inspector,frame,profile,templates)
        templates=dict(templates);masks=templates.get(row.get('template'))
        if masks is None:
            output.append(describe_ai_row(row,anchor));continue
        # Test the entire instance, including pixels beyond the local crop.
        # Clipping the AI mask to the ROI would hide an actual seating violation.
        allowed=np.zeros(frame.shape[:2],np.uint8)
        allowed[lo[1]:hi[1]+1,lo[0]:hi[0]+1]=masks['roi']
        raw_details={}
        row['ai_raw_seating']=containment(item['mask'],allowed,boundary_fraction=BOUNDARY_FRACTION,details=raw_details)
        row['ai_raw_seating_details']=raw_details
        observed=item['mask'] & visible_body
        row['ai_mask_dark_pixels']=int(np.count_nonzero(item['mask'] & ~visible_body))
        details={};seating=containment(observed,allowed,boundary_fraction=BOUNDARY_FRACTION,details=details)
        row['ai_seating']=seating;row['ai_seating_details']=details
        if seating in ('boundary','outside'):
            row['seating']=seating
            row.update(quality='outside' if seating=='outside' else 'abnormal',label='AI 마스크 안착 범위 이탈',reason='AI 부품 영역이 고정된 지그 경계를 벗어남 · '+row['reason'])
        elif seating=='missing':row=inconclusive(row,'AI 부품 영역 근거 부족')
        else:row['reason']='AI 종류·상태·영역 확인 + '+row['reason']
        output.append(describe_ai_row(row,anchor))
    return output


class ModelInspector:
    def __init__(self,geometry=None,provider=None):
        from .shape_inspection import ShapeInspector
        self.geometry=geometry if geometry is not None else ShapeInspector()
        self.provider=provider if provider is not None else RegionModel(class_ids=tuple(range(13)))
        self.stop=None
    def close(self):self.provider.close()
    def inspect(self,frame,anchors,profile):
        self.geometry.stop=self.stop
        rows=self.geometry.inspect(frame,anchors,profile)
        if not anchors or self.stop is not None and self.stop.is_set():return rows
        settings=inspection_settings(profile)
        if settings['method']!='model_roi':
            self.provider.close()
            return rows
        start=time.monotonic();detections=self.provider.predict(frame,settings);seconds=time.monotonic()-start
        rows=combine(rows,anchors,detections,self.geometry,frame,profile,settings['confidence'])
        for row in rows:row['ai_seconds']=seconds
        return rows


def inspector_for(profile):
    from .shape_inspection import ShapeInspector
    return ModelInspector() if inspection_settings(profile)['method']=='model_roi' else ShapeInspector()
