"""Read-only labels for accepted poses, separate from live image detections."""

def detection_reason(result):
    """Short rejection cause, including metadata retained during acquisition."""
    if not result or result.get('selected'):return ''
    error=result.get('error','');reason=result.get('rejection_reason') or result.get('cad_pallet_reason')
    if reason=='roi' or 'ROI' in error and ('벗어' in error or '경계' in error) or '모델 영역과 지정 범위가 겹치지' in error:return 'ROI 경계 이탈'
    reasons={'mask_too_small':'지그 마스크 면적 부족','mask_geometry':'마스크 형상 미확인',
             'projection':'평면 좌표 계산 불가','insufficient_visible_sides':'지그 측면 관측 부족',
             'cad_fit':'CAD 정합 기준 미달'}
    if reason in reasons:return reasons[reason]
    statuses={'orientation_unconfirmed':'방향 확인 중','ambiguous':'후보 여러 개',
              'calibration_mismatch':'카메라 보정 불일치','model_error':'AI 검출 오류'}
    if result.get('status') in statuses:return statuses[result['status']]
    if '지그 상단 테두리' in error:return '지그 상단 테두리 미확인'
    if '재탐색 대기' in error:return '지그 재탐색 대기'
    if result.get('model_detected') is False or result.get('status')=='model_not_found' and not result.get('model_detected'):return 'AI 지그 미검출'
    if result.get('model_detected'):return '지그 형상 미확인'
    return ''

def adoption_summary(text):
    """Stable short table labels; keep the original text in the detail area."""
    if ' · 채택 불가: ' in text:return text.split(' · 채택 불가: ',1)[1]
    if '실패' in text:return text.split(' · ')[0]
    if '회차 · 수집' in text:return text.split('회차')[0]+'회차 수집 중'
    if text.startswith('채택됨 · 유지'):return '채택 유지'
    if text.startswith('채택됨 · '):return text.split(' · ',1)[1]
    if text.startswith('확인 중'):return '관측 확인 중'
    if text.startswith('미채택 · '):return text.split(' · ',1)[1]
    return text

def adoption_text(result,now,hold_seconds):
    if not result:return '미채택'
    if result.get('selected'):
        if result.get('teaching_held'):return '채택됨 · 티칭 고정'
        if result.get('pose_frozen'):return '채택됨 · 실행 고정'
        if result.get('pose_held'):
            measured=result.get('pose_measured_at')
            remaining=max(0.,hold_seconds-(now-measured)) if measured is not None else max(0.,result.get('hold_remaining_s',0.))
            return f'채택됨 · 유지 {remaining:.1f}초' if remaining>0 else '만료 · 미채택'
        return '채택됨'
    reason=detection_reason(result)
    def explained(text):return text+' · 채택 불가: '+reason if reason else text
    if 'stable_candidate_seconds' in result:
        elapsed=result['stable_candidate_seconds'];required=result.get('stable_candidate_required_seconds',0.)
        if 'acquisition_seconds' in result:
            count=result.get('stable_candidate_samples',0)
            if result.get('acquisition_issue'):return explained(f"{result.get('acquisition_attempt',1)}회차 실패 · {result['acquisition_issue']} · 새 관측 {count}회")
            return explained(f"{result.get('acquisition_attempt',1)}/{result.get('acquisition_attempts_limit',3)}회차 · 수집 {elapsed:.1f}/{result['acquisition_seconds']:g}초 · 새 관측 {count}회")
        count=result.get('stable_candidate_samples');minimum=result.get('stable_candidate_required_samples')
        if elapsed>=required and count is not None and minimum is not None and count<minimum:return f'확인 중 {count}/{minimum}회'
        return f'확인 중 {elapsed:.1f}/{required:g}초'
    if reason:return '미채택 · '+reason
    return '미채택'


def drawable_adoptions(results,now,hold_seconds):
    from copy import deepcopy
    visible={}
    for key,result in results.items():
        item=result.get('selected') or {}
        if not item.get('quad') or not item.get('center_px'):continue
        if result.get('pose_held') and not (result.get('pose_frozen') or result.get('teaching_held')):
            measured=result.get('pose_measured_at')
            remaining=hold_seconds-(now-measured) if measured is not None else result.get('hold_remaining_s',0.)
            if remaining<=0:continue
        visible[key]=deepcopy(result)
    return visible
