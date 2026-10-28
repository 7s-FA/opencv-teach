# 실행 JSON 분석 기록

실행 기록은 기존 `data/episode-cli-runs/<run_id>.json`에 저장한다. 기존 `events[].status`, 시각, 소요시간, `timing_summary`는 유지한다. 분석용 실행 기록은 최상위 `schema_version: 2`, `record_kind: "execution"`과 `analysis.schema_version: 2`(기존 1도 읽기 지원)로 식별한다. 서버 알림·조회 기록이나 이전 형식은 이 표식이 없을 수 있다. 기존 원본 로그는 소급 변환하거나 덮어쓰지 않는다. 별도의 읽기 전용 집계기로 작업별 관측값을 정리할 수 있다.

## 집계 단위와 결과

- `analysis.job_id`: 한 실행의 공통 ID. 조립·적재 연속 작업과 정지 후 재개에서도 유지된다. ROS 관제로 시작한 작업은 기존 업무 요청 ID와 같다.
- `analysis.request_id`: 관제 업무 요청 ID. 직접 CLI 실행이면 null이다. 조회 RETRY/RETRY_CONTROL은 실행기를 새로 시작하지 않으므로 새 실행 기록을 만들지 않는다.
- 최상위 `run_id`: 팔별 기록 파일 ID. 조립에서 적재로 인계하면 바뀌므로 파일 수를 실행 횟수로 세면 안 된다. `analysis.runs`로 연결한다.
- 같은 `job_id`의 파일을 모아 `analysis.last_event_elapsed_s`가 가장 큰 스냅샷을 사용한다. 조립 파일은 `phase_completed: true`여도 전체 작업은 아직 `in_progress`일 수 있다. 전체 성공은 최종 DONE/CHAIN_DONE 기록으로만 확정한다.
- `category`: production(조립/적재), linear(리니어 단독), recovery(초기화/안전 복귀), unknown. 생산 성공률에는 해당 작업 유형만 포함한다.
- `outcome`: succeeded, failed, cancelled, interrupted, rejected, in_progress. 생산 도중 RESET을 마쳤다고 생산 성공으로 기록하지 않으며 cancelled로 구분한다. 독립 RESET/안전 복귀의 성공은 recovery 범주다.
- `record_complete: false`는 기록만으로 최종 결과를 확정할 수 없음을 뜻한다. 실행 중일 수도, 프로세스가 중단됐을 수도 있다. 성공이나 실패로 추정하지 않는다.
- 실행 잠금을 획득한 뒤 모델/에피소드 준비에 실패한 경우도 failed로 남긴다. 잠금 경합으로 실행을 시작하지 못한 요청은 rejected다. 준비 실패에서는 설정이나 지그 위치가 없을 수 있다.

## 시간과 동작

`events[].at_iso`/`at`는 달력 시각이다. 새 `at_monotonic_s`, `event.elapsed_monotonic_s` 및 analysis 내부 구간은 실행 PC/Pi의 단조 시계로 기록한다. `environment.boot_id`가 다른 절대 단조 시각은 빼지 않는다. 소요시간의 원점은 실행기 시작이며 PC에서 키를 누른 시점이나 ROS 접수 시점이 아니다.

`motion_calls`는 기존 move/play 호출 진입부터 검증된 반환 또는 예외까지의 관찰 구간이다. `requests`에는 실제 드라이버 요청 ID와 기존 RPC의 응답을 받은 시각이 들어간다. 요청 배열이 비어 있으면 드라이버 실행 요청 전에 중단된 호출이다. 준비 확인·정지·재개 시간이 포함될 수 있으며 순수한 모터 이동시간으로 해석하지 않는다.

`follower_observations`는 기존 상태 조회에서 상태/요청/진행 번호가 바뀐 경우의 관측이다. 추가 장치 조회를 하지 않는다. `actual_ticks`, 모터 moving 상태, 피드백 단조 시각을 보관하되 연속 고주파 궤적은 아니다. `STEP_DONE` 이벤트의 구조화된 `step`은 완료를 관측한 시점이며 물리적 도착 시각의 정밀 계측은 아니다.

`pause_intervals`는 보류 확인부터 재개/종료까지의 구간이다. `controls`는 관측 알림 횟수이고, 식별 가능한 제어 요청과 결과는 `control_requests`를 사용한다. `completed_without_intervention`은 최종 성공이며 기록된 정지 요청·재개·초기화 알림이 없다는 뜻이다.

`linear_intervals`는 기존 시간 기준 구동 시작/완료 관찰이다. 목표 mm를 실제 측정 위치로 취급하지 않는다. 팔·리니어·검사·모델 준비는 겹칠 수 있으므로 소요시간을 단순 합산하지 않는다.

## 지그 위치와 실행 조건

`jig_pose_sets`는 실행에 채택한 측정 묶음이다. 재측정할 때 새 `pose_set_id`를 만든다. X/Y(mm), yaw(deg)는 반올림 전 수치를 저장하고, 측정하지 않은 Z는 null로 둔다. 좌표계는 해당 팔의 보정 프로필이 정의하는 `profile_world`다. 서로 다른 팔·보정 버전의 좌표를 변환 없이 같은 위치로 묶지 않는다.

`plans` 및 `motion_calls`, 각 이벤트는 당시 `pose_set_id`와 `context_id`를 참조한다. 측정 실패로 채택한 좌표가 없으면 지그 묶음이 없거나 참조가 null이며, 이를 0mm 위치의 실패로 집계하지 않는다. 측정 시각, 시도 횟수, 원래 metric도 가능한 경우 저장한다.

`configurations`는 내용 해시를 키로 당시 에피소드, 프로필, 지그 정의, 보정 JSON, trim, 속도, 측정 설정, 메시 해시를 보관한다. 반복된 이벤트마다 설정을 복제하지 않는다. STL/모델 바이너리와 네트워크 제어 토큰은 수집하지 않는다. 모델 설정과 제공된 해시는 프로필에 보존되며, 실행 소스 버전은 `environment.runtime_sha256`로 식별한다.

## 검사와 오류

`inspection_by_arm.records`는 기존 검사 판정 기록이며 `expected_inspections`는 실행 시 정의된 검사 대상이다. `observed_passes`는 합격 기록이 관측됐다는 뜻이지 모든 검사나 제품 품질의 최종 합격을 보증하지 않는다. 검사 재시작/무효화 이벤트, 기대 항목, 최종 실행 결과를 함께 확인한다. 검사 미실시를 검사 합격으로 바꾸지 않는다.

`failure`는 오류 유형·원문·실패 단계, `artifacts.failure_evidence`와 검사 로그 경로는 기존 증거 파일 참조다. 이미지나 영상 수집량을 늘리지 않는다.

저장은 기존 순서 보장 비동기 JSON 기록기를 재사용한다. 중요한 기록의 저장 실패가 성공 통보를 막는 기존 동작은 유지한다. 설정/경로의 불변 사본은 저장 작업과 공유하고, 변경되는 결과와 구간은 이벤트 시점의 스냅샷으로 보관한다.


## 2026-10-23 추가: 중복 없는 집계와 누락 표시

### 작업당 한 개의 인덱스

`data/episode-job-index/<job_id>.json`은 한 작업의 최신 실행 기록을 가리킨다. `source_file`은 data 폴더 기준 상대 경로이고 `revision`은 공통 `analysis.event_count`다. 인덱스와 원본의 revision이 같은지 확인한다. 원본 저장 후 인덱스를 원자적으로 교체하므로 중간 종료 시 원본보다 인덱스가 늦을 수 있다. 이때 실행 파일들을 job_id로 묶어 최신 최종 기록을 선택한다.

`events[].event_id`와 `job_event_seq`는 팔 인계에도 유지되는 작업 전체 식별자·순서다. 기존 `event_seq`는 팔별 파일 안의 순서이므로 서로 다른 팔의 event_seq만 비교해 중복을 제거하지 않는다.

최종 실행 파일과 인덱스의 `accounting`은 파일 개수를 세지 않고 작업 결과, 고유 검사 항목, 도착 기록 커버리지와 구간 합집합을 제공한다. 상태 알림·조회·이전 비구조화 기록은 생산 횟수에 자동 포함하지 않는다. 서로 모순되는 최종 결과는 집계 충돌로 별도 보고한다.

### 실제 이동과 생략

`analysis.linear_requests`에는 목표 확인부터 이동 여부와 결과까지 기록한다.

- `already_at_target`: 기존 제어기가 목표 도착 상태로 응답해 이동을 생략. `movement_seconds: 0`.
- `moved`: STARTED→DONE 관측 구간. 위치 센서 실측이 아닌 기존 시간 기반 완료다.
- `checking`/`moving`으로 끝난 기록: 결과 미확정. 0초나 정상 완료로 바꾸지 않는다.

`readiness_checks`는 카메라·팔 연결·안전 자세의 시작과 확인을, `resource_preparation`은 검사 모델의 생성 요청·다음 단계용 사전 생성·기존 인스턴스 재사용을 기록한다. 모델 인스턴스가 있다는 사실과 준비 완료는 구분한다. 안전 자세의 `movement_skipped`와 `SAFE_POSE_SKIPPED:PREPARED_START_VALIDATED`는 검증된 준비 결과를 재사용했음을 나타낸다. 이 기록 추가로 준비나 이동 순서를 바꾸지는 않는다.

### 준비 계측과 병렬 구간

`analysis.preparation_profiles`는 profile_id별 최신 **누적** 스냅샷을 한 번만 보관한다. 조립·적재의 PREPARATION_PROFILE 문구를 합산하지 않는다. 상세 구간은 분석 객체에만 보관하고 문구에는 참조를 남긴다.

각 구간은 고유 interval_id와 시작·종료를 가진다. 모델·카메라·경로·검사가 겹치거나 포함 관계이면 합집합 길이를 사용한다. `accounting.time`은 관측된 구간의 합집합, 팔 호출과 리니어의 겹친 시간, 일시정지 구간을 별도로 제공한다. 구간이 덮지 않는 시간은 원인을 측정하지 않은 시간이며 유휴·CPU 대기로 단정하지 않는다. 팔 호출 구간도 실제 모터가 움직인 시간과 같지 않다.

구간은 profile당 최근 4096개로 제한하고 `dropped_intervals`로 누락을 밝힌다. 시각 없이 duration만 제공된 지표는 `without_interval_count`로 표시하며 합집합에 넣지 않는다. 소비한 프레임은 같은 source의 최근 256개 토큰으로 중복을 차단하고 퇴출 수를 `sample_tokens_evicted`에 남긴다.

### 모터 도착 기록과 재개

기존 상태 응답에 컨트롤러 세션 ID, 도착 순번, 최근 도착 16개를 포함한다. 추가 모터 조회·명령은 없다. 실행기는 이를 작업 기록에 누적해 진단 파일의 최근 200개 제한에만 의존하지 않는다. 누락된 수신 구간은 `arrival_transport_gaps`, 기대한 원래 스텝과 실제 관측 스텝 차이는 `accounting.arrival_coverage`로 표시한다.

`motion_calls.requests[].step_offset`은 재개 요청의 첫 스텝을 원래 경로에 연결한다. 도착 기록은 controller_session_id + arrival_seq로 중복을 제거하고, 팔·요청 ID·원래 스텝·목표 관절값으로 작업에 연결한다. 컨트롤러 재시작 뒤 재사용된 요청 번호는 이전 세션에 붙이지 않는다.

도착값의 `planned_target`은 원래 경로 목표, `target`은 집게 접촉 제한을 반영한 도착 기준, `servo_command`는 보정된 명령, `actual`은 읽은 관절값이다. `residual_ticks`는 actual−target이다. 집게 접촉으로 목표가 바뀐 차이를 그대로 추종 오류로 집계하지 않는다. `settled`, `pass_through`, `residual_accepted`는 각각 나눠 분석하며 이 값으로 외부 TCP·제품 치수 정확도를 주장하지 않는다.

### 검사와 성공 표본

`inspection_attempts`는 시작, 잠정 합격, 최종 합격, 무효화·재시작을 구분한다. 같은 검사 ID의 여러 시도를 제품 개수처럼 세지 않는다. 고유 기대 검사 ID와 최종 PASS 기록의 대조는 `accounting.inspection`에 있으며, 시스템 검사 결과가 사람/외부 계측의 정답 판정을 대신하지 않는다.

무개입 성공 집계는 최종 생산 성공, 기록 완결, 정지·재개·초기화 및 보류·supervision 손실·제어 요청 없음 조건을 사용한다. 성공 시간 분포와 성공률의 분모는 별개다. 성공률 계산에는 동일 조건의 실패·중단 기록도 포함해야 한다. 설정 ID·실행 버전이 다른 작업의 평균 시간은 자동으로 섞지 않는다.

### 읽기 전용 집계

```bash
python3 tools/summarize_job_logs.py data/episode-cli-runs --output verification/job-summary.json
```

이 명령은 장치·ROS에 연결하지 않는다. 기존 analysis v1 기록도 작업당 한 번 집계하지만 당시 없었던 이동 생략·직접 도착 기록은 `logging_available: false` 등으로 남긴다. 원본을 임의 추정으로 보충하지 않는다.

### 저장 오류와 응답 유실

실행 원본(`episode-cli-runs`) 저장은 완료 통보의 필수 조건이다. 파생 색인(`episode-job-index`)만 저장하지 못한 경우에는 경고를 남기고 원본의 확정 결과를 유지한다. 색인 독자는 참조 원본의 revision을 검증해야 하며, 최신 여부를 확인할 수 없으면 원본을 묶어서 읽는다. 위 집계 도구는 원본 파일을 사용한다.

`motion_calls.send_attempts`는 RPC 호출 직전에 남긴 전송 시도이며 실제 장치 접수의 증거는 아니다. 응답을 받은 요청은 `request_id`를 채우고 기존 `requests`에도 기록한다. 초기·재개 전송의 응답이 유실되면 해당 목표 스텝을 기대 관측에 포함하고 `unacknowledged_send_attempts`와 `coverage_state: indeterminate`로 표시한다. 시도 정보가 없는 과거 기록 중 요청 ID 없이 종료된 이동도 불확실 상태로 분류한다. 로그 보완을 위한 이동 재전송은 하지 않는다.
