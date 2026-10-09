"""Read-only labels for accepted poses, separate from live image detections."""

def adoption_summary(text):
    """Stable short table labels; keep the original text in the detail area."""
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
    if 'stable_candidate_seconds' in result:
        elapsed=result['stable_candidate_seconds'];required=result.get('stable_candidate_required_seconds',0.)
        if 'acquisition_seconds' in result:
            count=result.get('stable_candidate_samples',0)
            if result.get('acquisition_issue'):return f"{result.get('acquisition_attempt',1)}회차 실패 · {result['acquisition_issue']} · 새 관측 {count}회"
            return f"{result.get('acquisition_attempt',1)}/{result.get('acquisition_attempts_limit',3)}회차 · 수집 {elapsed:.1f}/{result['acquisition_seconds']:g}초 · 새 관측 {count}회"
        count=result.get('stable_candidate_samples');minimum=result.get('stable_candidate_required_samples')
        if elapsed>=required and count is not None and minimum is not None and count<minimum:return f'확인 중 {count}/{minimum}회'
        return f'확인 중 {elapsed:.1f}/{required:g}초'
    if result.get('status')=='orientation_unconfirmed':return '미채택 · 방향 확인 중'
    if result.get('status')=='ambiguous':return '미채택 · 후보 여러 개'
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
