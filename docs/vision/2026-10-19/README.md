# 지그 인식·안착 검사 갱신 · 2026-10-19

현재 작업대의 PC·Pi에는 **지그별 위치 정합과 첫 3회 조기 채택**, **YOLO 라벨 우선 + OpenCV·CAD 안착 검사**가 적용돼 있다. 지그 위치를 확정하는 단계와 부품을 검사하는 단계는 별개다. 오늘 마지막 배포 확인에서 PC–Pi 실행 파일 120개가 일치했다.

- [지그 인식·빠른 위치 채택](jig-adoption.md)
- [YOLO·OpenCV 안착 검사와 오류 판정](inspection.md)
- [검증 결과와 남은 확인](validation.md)
- [오늘 업무 일지](../../progress-2026-10-19.md)

## 현재 적용 상태

| 구분 | 적용 방식 |
|---|---|
| 운반용 지그 위치 | YOLO 마스크로 초기·재탐색 → OpenCV 외곽·격자·360° 방향 검증 → 정상 추적 → 첫 3회 품질·안정성 확인 |
| 완제품 팔레트 위치 | 매 관측의 새 YOLO 마스크 → 고정 CAD 외형 정합 → 첫 3회 일치 확인 |
| 부품 종류·조립 상태 | 신뢰도 기준을 만족하는 유일한 YOLO 후보의 라벨 우선 |
| 안착 여부 | 해당 라벨의 OpenCV·CAD 형상·중심·각도·삽입·경계 기준 확인 + AI 마스크 경계 검사 |
| 검사 방식 선택 | 부품·조립 검사에서 OpenCV / AI + OpenCV를 별도로 적용. 지그 검출 설정과 독립 |

## 배포·검증 범위

PC·Pi 실사용본의 적용 기록과 이 저장소의 공개 소스 동기화를 구분한다. 공개 저장소는 Windows·Linux 오프라인 데모의 파일·경로·렌더링 호환 처리를 유지한다. 개인 장치 연결 설정, 모델 실행 환경, 학습 가중치와 원본 전체 데이터셋은 포함하지 않는다. `assets/models/*/manifest.json`은 모델 파일 검증 정보이며 가중치가 설치됐다는 뜻이 아니다.

현재 작업대는 YOLO26n NCNN FP16·신뢰도 0.5로 검증했다. AI 인식 화면의 모델 비교 기능과 실제 지그·안착 검사 경로의 n/s 선택을 구분한다. YOLO26x·SAM3의 GPU 벤치마크 수치를 Pi 지그 검사 속도로 사용하지 않는다.

기존 실물 작업 성공 기록은 보존한다. 오늘 최종 검사 수정 후 A/B 조립·운반 전체 에피소드를 다시 성공시켰다는 의미는 아니다. 운반용 지그의 최종 격자 수정은 저장 영상 재생으로 확인했고 새 실물 양성 검증은 남아 있다.

## 관련 코드

- [위치 누적·조기 채택](../../../SO101_teach/so101_teach/jig_consensus.py)
- [운반용 지그 품질 조건](../../../SO101_teach/so101_teach/two_frame_gate.py)
- [팔레트 CAD 정합](../../../SO101_teach/so101_teach/cad_pallet.py)
- [AI 라벨과 안착 검사 결합](../../../SO101_teach/so101_teach/ai_inspection.py)
- [검사 방식 선택 화면](../../../SO101_teach/so101_teach/camera_inspection_ui.py)

위 코드 링크는 저장소 루트를 기준으로 확인한다. 검증 원본은 작업대의 `SO101_teach/verification/*-20261019/`에 보관하며, 공유용 핵심 수치와 제한은 이 문서에 정리했다.

## Confluence 설명과 업무 일지

- [지그 인식·빠른 위치 채택](https://rorobot.atlassian.net/wiki/spaces/robot3/pages/39714835)
- [YOLO·OpenCV 안착 검사와 오류 판정](https://rorobot.atlassian.net/wiki/spaces/robot3/pages/39944193)
- [10/19 업무 정리](https://rorobot.atlassian.net/wiki/spaces/robot3/pages/39714852)
