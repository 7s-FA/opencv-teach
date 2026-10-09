"""Episode-owned visual inspection checkpoints; no actuator or camera access."""
from copy import deepcopy
import math

STATIONS={'carrier':'운반용 지그','linear':'리니어 조립','finished':'완성품 팔레트'}
TARGETS={'carrier':{'housing':'하단 부품','insert':'중단 부품','cap':'상단 부품'},
         'linear':{'1':'좌측 · A용 팔레트','2':'우측 · B용 팔레트'},'finished':{'pallet':'완성품 팔레트'}}
EXPECTED={'present':'지정 부품 안착','housing_seated':'하단 안착','insert_added':'중단 삽입','cap_added':'상단 결합 · 완성품','empty':'부품 없음','occupied':'부품 있음 · 안착 별도'}
STATES={'carrier':('present','empty'),'linear':('empty','occupied','housing_seated','insert_added','cap_added'),'finished':('empty','occupied','cap_added')}
START_TARGETS={**TARGETS,'carrier':{'all':'선택 완제품의 부품 전체 (3개)','all_products':'A/B 부품 전체 (6개)',**TARGETS['carrier']}}


def validate_check(check,*,startup=False):
    if not isinstance(check,dict) or set(check)-{'station','target','expected','timeout_seconds','minimum_observations'} or not {'station','target','expected','timeout_seconds'}<=set(check):raise ValueError('안착 검사 설정 형식 오류')
    minimum=check.get('minimum_observations',2)
    if type(minimum) is not int or not 2<=minimum<=10:raise ValueError('최소 연속 판정 횟수는 2~10회입니다.')
    station=check['station'];targets=START_TARGETS if startup else TARGETS
    if not isinstance(station,str) or station not in STATIONS:raise ValueError('안착 검사 위치 오류')
    if not isinstance(check['target'],str) or not isinstance(check['expected'],str) or check['target'] not in targets[station] or check['expected'] not in STATES[station]:raise ValueError('검사 위치에 맞는 대상과 기대 상태를 선택하세요.')
    if check['target'] in ('all','all_products') and check['expected']!='present':raise ValueError('운반대 부품 전체 검사는 지정 부품 안착을 선택하세요.')
    timeout=check['timeout_seconds']
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 2<=timeout<=30:raise ValueError('안착 검사 제한시간은 2~30초입니다.')


def validate_inspection(episode):
    if episode.get('inspection_timing') not in (None,'step_complete'):raise ValueError('지원하지 않는 검사 시점 형식입니다.')
    product=episode.get('product_type')
    if product is not None and product not in ('A','B'):raise ValueError('완제품 종류는 A 또는 B로 선택하세요.')
    startup=episode.get('startup_inspections',[])
    if not isinstance(startup,list) or len(startup)>12:raise ValueError('시작 전 검사는 목록으로 최대 12개까지 지정하세요.')
    if has_inspections(episode) and product not in ('A','B'):raise ValueError('안착 검사를 사용하려면 에피소드의 완제품 종류를 선택하세요.')
    for check in startup:validate_check(check,startup=True)
    for step in episode.get('steps',[]):
        if 'inspection' not in step:continue
        check=step['inspection']
        if product not in ('A','B'):raise ValueError('안착 검사를 사용하려면 완제품 종류를 선택하세요.')
        if step.get('safe_boundary'):raise ValueError('안전 자세에는 안착 검사를 지정할 수 없습니다.')
        validate_check(check)


def has_inspections(episode):return bool(episode.get('startup_inspections')) or any('inspection' in step for step in episode.get('steps',[]))


def check_product(check,product):
    return {'1':'A','2':'B'}[check['target']] if check['station']=='linear' else product


def startup_steps(episode):
    """Expand all-parts checks without adding movement steps to the saved episode."""
    rows=[]
    for i,check in enumerate(episode.get('startup_inspections',[])):
        products=('A','B') if check['target']=='all_products' else (episode['product_type'],)
        targets=('housing','insert','cap') if check['target'] in ('all','all_products') else (check['target'],)
        for product in products:
            for target in targets:
                individual={**check,'target':target};product_for_check=check_product(individual,product)
                rows.append({'id':f'start-{i}-{product}-{target}','name':f'시작 전 · {product_for_check} · {STATIONS[check["station"]]} · {TARGETS[check["station"]][target]}','inspection':individual,'inspection_product':product_for_check})
    return rows


def segments(steps,targets):
    if len(steps)!=len(targets):raise ValueError('스텝과 실행 경로 길이가 다릅니다.')
    result=[];start=0
    for i,step in enumerate(steps):
        if 'inspection' in step or i==len(steps)-1:
            result.append({'steps':deepcopy(steps[start:i+1]),'targets':deepcopy(targets[start:i+1]),'check':deepcopy(step.get('inspection'))})
            start=i+1
    return result


def migrate_inspection_timing(episode):
    """Move legacy checks to their actual trigger step once, preserving timing."""
    if episode.get('inspection_timing')=='step_complete':return episode
    if episode.get('inspection_timing') is not None:raise ValueError('지원하지 않는 검사 시점 형식입니다.')
    value=deepcopy(episode);steps=value.get('steps',[])
    from .episode_events import events_for
    events=events_for(value);original_events=events.copy();moved_events=False
    checks=[(i,step.pop('inspection')) for i,step in enumerate(steps) if 'inspection' in step]
    for i,check in checks:
        if i+1>=len(steps) or steps[i+1].get('safe_boundary'):
            raise ValueError('기존 검사의 실제 완료 스텝을 확인하세요: '+steps[i].get('name',''))
        steps[i+1]['inspection']=check
        for kind,key in original_events.items():
            if key==steps[i]['id']:events[kind]=steps[i+1]['id'];moved_events=True
    if moved_events:value['completion_events']=events
    value['inspection_timing']='step_complete'
    return value


def inspection_schedule(steps):
    """Start each check immediately after its own step is confirmed complete."""
    return [(i+1,deepcopy(step)) for i,step in enumerate(steps) if 'inspection' in step]


def inspection_jig_ids(steps):
    stations={s['inspection']['station'] for s in steps if 'inspection' in s}-{'linear'}
    if not stations:return set()
    import json
    from .domain import ROOT
    reference=json.loads((ROOT/'inspection/roi_reference.json').read_text())
    return {reference['stations']['carrier' if station=='carrier' else 'finished_pallet']['jig_id'] for station in stations}


def fixed_jig_results(poses):
    """Adapt the execution snapshot without measuring or moving its coordinates."""
    result={}
    for key,value in poses.items():
        x,y,yaw=value['pose'];symmetry=value.get('symmetry_deg',90)
        metric={'center_xy_mm':[x,y],'yaw_deg':yaw,'symmetry_deg':symmetry}
        if 'mesh_yaw_offset_deg' in value:metric['mesh_yaw_offset_deg']=value['mesh_yaw_offset_deg']
        result[key]={'selected':{'metric':metric,'orientation_verified':symmetry==360},'pose_frozen':True}
    return result


def target_anchor(anchor,check,product):
    if check['station']=='carrier':return anchor.get('product')==product and anchor.get('part')==check['target']
    if check['station']=='linear':return anchor['id'].endswith('_'+str(int(check['target'])-1))
    return anchor.get('part')=='finished'


def inspection_reason(row):
    state=row.get('state')
    observed={'cap_visible':'상단 형상 · 높이 미확인','cap_only':'상단 단품 · 미완성','empty':'부품 없음','present':'부품 있음','housing_seated':'하단 있음','insert_added':'하단·중단 있음','cap_added':'상단 결합 형상','wrong_part':'다른 부품','unknown':'판정 불가','checking':'판정 중'}.get(state,row.get('label') or '판정 불가')
    product=(row.get('product','')+' ') if row.get('product_certain') and state not in ('empty','unknown','checking') else ''
    parts=['관측 '+product+observed,row.get('reason') or row.get('label') or '판정 불가']
    position=row.get('target_position_mm')
    if position is not None:
        parts.append('검사 중심(로봇 기준) '+', '.join(f'{axis} {value:+.1f}' for axis,value in zip(('X','Y','Z'),position))+' mm')
    offset=row.get('center_offset_mm')
    if offset is not None:
        parts.append(f'슬롯 기준 편차 X {offset[0]:+.1f}, Y {offset[1]:+.1f} mm · 거리 {row["center_error_mm"]:.1f} / 허용 {row["center_tolerance_mm"]:g} mm')
    elif row.get('offset_mm') is not None:
        x,y=row['offset_mm'];parts.append(f'형상 정합 편차(지그 축) X {x:+.1f}, Y {y:+.1f} mm')
    if 'outside_percent' in row:
        parts.append(f'ROI 밖 {row["outside_percent"]:.2f}%')
    if 'boundary_percent' in row:
        parts.append(f'경계 접촉 {row["boundary_percent"]:.2f}% / 기준 {row["boundary_tolerance_percent"]:g}%')
    return ' · '.join(parts)


class InspectionDecision:
    """Require consecutive fresh frames after arrival, never a retained UI result."""
    def __init__(self,check,product,started,*,frame_gap=1,ignore_missing=False,evidence=None):
        self.check=deepcopy(check);self.product=check_product(check,product);self.started=started;self.last_at=None;self.signature=None;self.count=0;self.reason='새 영상 판정 대기'
        self.frame_gap=frame_gap
        self.ignore_missing=ignore_missing;self.evidence=evidence;self.basis=None
    def observe(self,result,now):
        if result and result.get('error'):raise ValueError('안착 검사 오류: '+result['error'])
        if result:
            at=result.get('at');row=result.get('row')
            if type(at) in (int,float) and self.started<=at<=now and now-at<3 and (self.last_at is None or at>self.last_at):
                if self.last_at is not None and at-self.last_at>self.frame_gap:self.count=0;self.signature=None
                self.last_at=at
                if row:
                    self.reason=inspection_reason(row)
                    signature=(row.get('quality'),row.get('state'),row.get('product'),row.get('product_certain'))
                    self.count=self.count+1 if signature==self.signature else 1;self.signature=signature
                    ambiguous_top=(self.product=='B' and self.check['station']=='linear' and self.check['expected']=='cap_added' and row.get('state')=='cap_visible')
                    good=(row.get('quality')=='normal' and row.get('state')==self.check['expected'] and row.get('product')==self.product and row.get('product_certain') is True)
                    if ambiguous_top:
                        good=(row.get('quality')=='normal' and row.get('product')=='B'
                              and row.get('product_certain') is True and row.get('seating')=='inside')
                        self.basis={'basis':'current_visual','state':'cap_visible'} if good else None
                        self.reason='현재 B 형상·안착 위치 확인'+('' if good else ' 실패')
                    if self.check['expected']=='empty':good=row.get('state')=='empty' and row.get('quality') not in ('waiting','outside') and row.get('seating') in (None,'missing')
                    if self.check['expected']=='occupied':good=row.get('state') in ('present','housing_seated','insert_added','cap_added','cap_visible','wrong_part') and row.get('quality') not in ('waiting','outside') and row.get('seating') not in ('outside','missing')
                    if self.count>=self.check.get('minimum_observations',2):
                        if good:return True
                        if row.get('quality') in ('abnormal','outside') or not ambiguous_top and row.get('quality')=='normal' and (row.get('product_certain') is True or self.check['expected'] in ('empty','occupied')):
                            raise ValueError('안착 불합격: '+self.reason+' · 기대 '+self.product+' '+EXPECTED[self.check['expected']])
                else:
                    if not self.ignore_missing:self.count=0;self.signature=None
                    self.reason=result.get('reason','검사 위치 확인 불가')
        if now-self.started>=self.check['timeout_seconds']:raise ValueError('안착 판정 시간 초과: '+self.reason)
        return False


class GroupInspectionDecision:
    """Independent valid-observation counts for a shared image calculation."""
    def __init__(self,steps,started,*,evidence=None):
        self.evidence=evidence
        self.steps={s['id']:deepcopy(s) for s in steps}
        self.pending={key:InspectionDecision(s['inspection'],s['inspection_product'],started,
                      frame_gap=s['inspection']['timeout_seconds'],ignore_missing=True,evidence=evidence) for key,s in self.steps.items()}
    def observe(self,result,now):
        if result and result.get('error'):raise ValueError('안착 검사 오류: '+result['error'])
        rows=(result or {}).get('results',{});passed=[]
        for key,decision in list(self.pending.items()):
            observation=deepcopy(rows.get(key));row=(observation or {}).get('row')
            if row and (row.get('quality')=='waiting' or row.get('state') in ('unknown','checking')
                        or decision.check['expected'] not in ('empty','occupied') and row.get('quality') not in ('abnormal','outside') and not row.get('product_certain')):
                observation['row']=None;observation['reason']=inspection_reason(row)
            try:done=decision.observe(observation,now)
            except ValueError as exc:raise ValueError(self.steps[key]['name']+' · '+str(exc)) from exc
            if done:
                step=self.steps[key]
                if decision.basis:step['inspection_basis']=deepcopy(decision.basis)
                if self.evidence:self.evidence.record(step,decision.product)
                passed.append(step);del self.pending[key]
        return passed


def criteria_signature(root=None):
    """An exported checkpoint must use identical algorithms and CAD criteria."""
    from pathlib import Path
    import hashlib
    if root is None:
        from .domain import ROOT
        root=ROOT
    root=Path(root);digest=hashlib.sha256()
    files=('so101_teach/assembly_evidence.py','so101_teach/episode_inspection.py','so101_teach/episode_inspection_runtime.py','so101_teach/shape_inspection.py','so101_teach/inspection_geometry.py','so101_teach/inspection_seating.py','inspection/roi_reference.json','inspection/shape_templates.json','inspection/shape_templates.npz','inspection/appearance_reference.json')
    for name in files:
        path=root/name
        if not path.is_file():raise ValueError('안착 검사 기준 파일 없음: '+name)
        digest.update(name.encode());digest.update(path.read_bytes())
    return digest.hexdigest()
