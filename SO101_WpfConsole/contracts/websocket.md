# 관제 통신 규약 v1

`openapi.json`은 REST 정의, `protocol.schema.json`은 공통 메시지 정의입니다. `scripts/generate_contracts.py`가 함께 생성합니다. `examples`의 기본 DTO는 실제 C# 직렬화 테스트에서 출력하며, 통신 시험에서 실제 Python 응답과 WS 프레임도 스키마로 검증합니다.

## REST

- HTTPS + Bearer 인증. 토큰은 URL에 넣지 않습니다. `POST /v1/session` 본문은 `{"protocol_version":1}`이며 단일 조작 세대 `session_generation`을 반환합니다.
- 후속 요청은 `X-Session-Generation` 헤더를 포함합니다. 명령 본문의 세대와 일치해야 합니다. 이미 연결된 세션·이벤트 채널은 중복 생성할 수 없습니다.
- `/v1/commands`는 JOB/CONTROL만 받습니다. HELP/STATUS/QUIT는 UI/세션 기능, RETRY/RETRY_CONTROL은 `/v1/requests/{request_id}` 조회입니다.
- `202 ACCEPTED`는 중계 접수이며 로봇 접수·성공이 아닙니다. 동일 ID/명령은 `200 EXISTING_REQUEST`, 다른 명령에서 같은 ID를 재사용하면 `409 ID_CONFLICT`입니다.
- 일반 실행은 요청 영수증과 기존 ClientJournal 저장을 마친 뒤 ROS로 보냅니다. 정지는 디스크 작업과 별도 대기열을 통해 먼저 전송합니다. 저장 실패·세션 만료·정지 순번 변경·2초 전송 대기 만료 시 일반 실행을 보내지 않습니다.
- 미확정 요청은 원래 ID로 조회합니다. API/앱/중계 재시작 후 POST를 자동 재전송하지 않습니다. 중계 404는 Pi의 미접수 증거가 아니며, 자동 새 실행을 허용하지 않습니다.
- Windows에 원래 명령과 대상이 남아 있고 API 영수증만 없으면 `command`·`arm` 조회 매개변수로 같은 ID를 Pi에 조회합니다. 정지 대상 힌트가 오래된 경우 접수되지 않은 조회만 다른 팔에도 확인합니다. 이 경로는 execute를 호출하지 않습니다.
- 조회 응답은 짧게 ROS 결과를 기다리되 네트워크·ROS 이벤트 루프를 막지 않습니다. 현재 슬롯에서 벗어난 오래된 ID도 영수증으로 조회할 수 있습니다.
- `BUILD_DONE`은 중간 진행입니다. `IDLE/success` Action Result만 `SUCCEEDED`가 됩니다. 작업과 제어의 상태·결과는 분리합니다. `PENDING`은 완료가 확정되지 않은 상태이며 현재 진행을 확인할 수 없으면 `UNKNOWN`으로 표시합니다.
- `Snapshot.storage_error`는 저장 장애, `active_arm`은 기존 busy/active-arm 규칙으로 결정한 제어 대상입니다. 연결 상태와 요청 상태를 혼합하지 않습니다.

## WebSocket

`/v1/events`는 같은 인증·세대 헤더를 사용합니다. 상태는 `GatewayEvent`의 전체 스냅샷입니다. 250ms 간격으로 현재 상태를 갱신하며 모든 스냅샷에 증가하는 sequence가 붙습니다. 각 프레임이 완전한 기준 상태이므로 sequence 간격은 해당 프레임으로 동기화됩니다. 역전·중복 프레임은 무시하고, 중계/세대가 바뀌면 새 연결에서 동기화합니다.

로그나 영상을 이 채널에 섞지 않습니다. 서버 수신 메시지는 4KiB, WPF 수신 메시지는 64KiB로 제한합니다. WS 연결을 다시 붙일 때 과거 실행 명령이나 이벤트 큐를 재생하지 않습니다.

Pi challenge는 `SupervisionFrame`의 외부 구분자를 사용합니다.

```json
{"type":"supervision_challenge","payload":{"protocol_version":1,"session_generation":"00000000000000000000000000000001","bridge_instance_id":"00000000000000000000000000000002","arm":"arm2","epoch":"epoch-value","nonce":"nonce-value","expires_in_ms":1000}}
```

WPF는 UI Dispatcher 응답 후 `type=supervision_ack`와 `SupervisionAck` payload로 돌려줍니다. epoch·nonce·arm·세션·중계 ID는 같고 `expires_in_ms`는 응답에서 제외합니다. 중계는 일치·만료·재사용 여부를 확인한 경우에만 기존 ROS heartbeat를 보냅니다. Pi의 0.25초 challenge와 3초 생존 만료는 유지합니다.

Pi가 없어도 관제 세션 자체의 응답성을 확인하기 위해 별도의 `UiLeaseChallenge`/`UiLeaseAck`를 사용합니다.

```json
{"type":"lease","protocol_version":1,"bridge_instance_id":"00000000000000000000000000000002","session_generation":"00000000000000000000000000000001","nonce":"00000000000000000000000000000003","expires_in_ms":1000}
```

UI Dispatcher 응답 후 `type=lease_ack`으로 같은 필드를 반환하되 `expires_in_ms`는 제외합니다. 유효 응답이 3초 끊기면 세션을 만료시키고 생존 응답을 중단합니다. 이 프레임은 Pi challenge 응답을 대신하지 않습니다.

WPF 종료·UI 정지·VM pause·가상 NIC 단절·중계 종료에서 생존 응답이 중단됩니다. 복구 시 새 세션과 fresh challenge를 사용하며, 미확정 ID는 조회만 합니다. 자동 RESTART나 자동 새 업무 실행은 하지 않습니다.
