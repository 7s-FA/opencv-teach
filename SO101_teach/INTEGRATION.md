# 조립대 명령과 관제 Action

Pi `192.0.2.10`, 사용자 `ubuntu`, ROS 2 Jazzy / `ROS_DOMAIN_ID=40`.
새 SSH 터미널에서는 `.bashrc`가 `/home/ubuntu/project/episode_ros_env.bash`를 불러옵니다. 이미 열려 있던 터미널은 이 파일을 한 번 다시 source합니다. 파일을 불러오는 것만으로 장비가 움직이지 않습니다.

| 터미널 명령 | 동작 | 관제 Action |
|---|---|---|
| `build_a`, `build_b` | 팔2 A/B 조립, 리니어 전진 100mm | `/arm2/data` |
| `load_a`, `load_b` | 팔3 A/B 팔레트 적재, 리니어 후진 1.5mm | `/arm3/data` |
| `slide_build` | 리니어 전진 100mm | `/arm2/data` |
| `slide_load` | 리니어 후진 1.5mm | `/arm3/data` |
| `estop` | 팔 정지·작업 보류, 리니어는 기존 목표까지 계속 진행 | `EMER_STOP` |
| `restart` | 비상정지 후 남은 작업 재개 | `RESTART` |
| `reset` | 비상정지 작업 취소 → 안전 자세 복귀 → 토크 OFF | `RESET` |
| `build_status`, `load_status` | 마지막 실행 기록 조회 | — |
| `slide_status` | 현재 리니어 제어기 상태 조회 | — |
| `work_status` | 현재 CLI/Action 작업 상태와 최근 상세 알림 조회 | — |
| `work_watch` | 현재 작업 상태 변경과 새 상세 알림을 계속 표시 | — |
| `work_logs` | Action 서버의 최근 로그와 새 오류·알림을 계속 표시 | — |

이미 열려 있는 Pi 터미널은 `source /home/ubuntu/project/episode_ros_env.bash` 후 위 명령을 사용합니다. `work_watch`는 최근 알림 15개를 먼저 보여주며 `work_watch --tail 30`으로 개수를 바꿀 수 있습니다. 과거 완료 기록과 현재 상태는 구분해서 표시합니다. 조회 화면의 Ctrl+C는 조회만 종료하며 실행 중인 로봇 작업에는 영향을 주지 않습니다. 연결 점유·설정 오류 등 실행 준비 중의 거절도 REJECTED 이력으로 표시하며, 다른 실행 중 작업의 현재 상태는 덮어쓰지 않습니다. 티칭 앱의 수동 조작은 이 조회의 대상이 아닙니다. 서비스 상태는 시작 시 표시하고 이후 5초마다 확인해 변경될 때만 알립니다.

일반 `stop` 명령은 사용하지 않습니다. `reset` 셸 함수는 조립대 초기화 명령입니다. 터미널 자체를 초기화하려면 `/usr/bin/reset`을 사용합니다. 이전 `arm2_a/b`, `arm3_a/b`, `linear_f/r`는 새 명령을 부르는 별칭으로 남겼습니다. 임의의 리니어 세부 위치 입력은 지원하지 않습니다.

직접 실행 예:

```bash
build_a
# 또는
/home/ubuntu/OpenCV_teach/run_episode.sh build_a

# 동작 없이 파일·보정·목표 위치만 확인
/home/ubuntu/OpenCV_teach/run_episode.sh build_a --check
```

### work_watch 알림 범위

- Action 서버 준비, 명령 수신·접수, 잘못된 명령·다른 팔 명령·중복 작업 거절
- CLI 준비 실패와 연결 점유, 지그 검출·확정 좌표, 리니어 이동·완료, 경로 계산
- 시작 전 검사·스텝 검사 시작/통과/실패, 스텝·단계 완료, 작업 완료·토크 해제
- 정지·재개·초기화 요청/응답/실제 완료 또는 실패, 활성 작업 없음과 확인 시간 초과
- 실행기 시작 실패·진단 출력, Action 최종 결과, 서비스 상태 변화, 이력 읽기 오류

고빈도 모터 수신값은 출력하지 않습니다. 기존 CLI 상세 이벤트는 중복 저장하지 않고, Action 접수·최종 결과는 별도 알림으로 구분합니다. 초기 거절과 제어 알림은 실행 중인 작업의 현재 상태 파일을 덮어쓰지 않습니다. 프로그램 갱신 전에 열어 둔 `work_watch`는 Ctrl+C로 조회만 종료한 뒤 다시 실행하면 서비스 변화 감시까지 적용됩니다.

## 실행과 비상정지

명령은 두 팔의 Pi 제어 연결과 리니어 사용권을 예약합니다. 다른 티칭 창이나 작업이 사용 중이면 거절하며 대기열에 넣지 않습니다. PC 티칭 앱을 종료하고 전원·USB·카메라·부품·지그 및 이동 경로를 준비합니다. 실제 장애물 회피는 하지 않습니다. 에피소드에 지정한 부품 검사는 아래 검사 실행 순서에 따라 수행합니다.

정상 순서: 연결·현재 상태 확인 → 리니어 이동과 고정 지그 검출을 병행 → 두 조건 모두 완료 → 팔 토크 준비·시작 안전 자세 → 보정 경로 계산 → 에피소드 → 종료 안전 자세·마지막 검사 완료 → 토크 해제 요청 → 완료 반환·다음 명령 허용. 시작 안전 자세 이동도 준비 완료 이후에 수행합니다.

`estop` 또는 Pi의 작업 실행 터미널 Ctrl+C는 로봇팔과 다음 작업 진행을 보류합니다. 이미 시작한 리니어는 원래 목표까지 계속 움직이며 재출력·타이머 초기화·재개 시간 가산을 하지 않습니다. `PAUSED`는 팔/작업 보류를 뜻하며 리니어의 물리 정지를 뜻하지 않습니다. 이미 켜진 팔 토크는 유지하고 아직 준비 전인 팔은 토크 OFF를 유지합니다. 자동으로 안전 자세로 돌아가지 않습니다. `PAUSED`가 확인된 뒤 `restart` 또는 `reset`을 사용합니다. 원래 실행 프로세스와 Action Goal은 계속 살아 있습니다.

- `restart`: 완료한 스텝은 반복하지 않습니다. 중단된 목표부터 남은 경로를 현재 유지 자세에서 재계산해 이어갑니다. 이미 확정한 보정 목표를 유지합니다. 준비 단계에서 멈췄다면 먼저 얻었던 고정 지그 검출 결과도 재개 후 다시 측정합니다. 리니어에는 재개 명령을 보내지 않습니다.
- `reset`: 남은 에피소드를 취소하고 두 팔 모두 저장된 종료 안전 자세로 순차 복귀한 뒤 각각 토크를 해제합니다. 리니어가 진행 중이면 최초 명령 완료를 기다린 뒤 팔의 안전 복귀를 수행합니다. 리니어 단독 작업도 목표 완료를 기다린 뒤 작업 취소 결과를 반환합니다. 복귀 중 `estop`과 `restart`도 적용됩니다. 안전 자세 누락·복귀 실패 시 자동으로 토크를 해제하지 않습니다.
- 통신/드라이버 오류는 사용자 비상정지와 구분하며 자동 재개하지 않습니다. 실행 프로세스가 종료되면 자동으로 이전 에피소드를 재실행하지 않습니다.

안착 불합격 등으로 실행 프로세스가 이미 종료된 경우에도 `reset`을 사용할 수 있습니다. 실패 당시의 안전 자세·팔·영점 정보를 보존하며, 명시적 reset 요청에만 안전 복귀 후 토크를 해제합니다. 일반 `reset`과 ROS의 `/arm2/data`·`/arm3/data` RESET은 모두 조립대 전체 초기화입니다. 두 팔의 저장된 안전 자세·영점을 먼저 검증하고, arm2와 arm3를 순차적으로 안전 자세로 복귀시켜 토크 OFF까지 확인합니다. 진행 중인 작업을 취소한 경우에는 작업 중이던 팔부터 복귀합니다. 두 팔 모두 끝나야 RESET_DONE과 성공 결과를 반환합니다. 복귀 중에도 비상정지할 수 있으며, 다른 작업 실행 중·영점 변경·리니어 상태 미확인·안전 자세 누락이면 복귀하지 않습니다. 새 작업을 시작하면 그 팔의 이전 복귀 기록은 새 작업의 안전 자세 정보로 교체합니다. 토크가 꺼져 있어도 최신 자세 확인 후 토크를 켜고 안전 자세로 복귀한 다음 다시 토크를 해제합니다. 이미 reset을 완료한 뒤에도 저장된 복귀 정보를 다시 사용할 수 있습니다.

정상 작업은 종료 코드 0, reset으로 취소된 작업은 130, 실패는 1입니다. 제어 명령의 `…_REQUESTED`는 접수이고 실제 정지·복귀·완료는 Pi 터미널의 상세 알림으로 확인합니다. 관제의 제어 Goal 결과는 실제 정지·재개·초기화 확인 후 반환합니다.

## 리니어 상태와 시간 기준 완료

전진 100mm와 후진 1.5mm만 사용합니다. 초기 구동 시간은 8초입니다. 동일한 목표의 `TIMED_COMPLETE` 기록이 있으면 재출력하지 않으며, 기록이 없거나 불확실하면 전체 시간으로 준비합니다.

| `position_state` | 의미 |
|---|---|
| `FORWARD` | 전진 목표 구동 시간 완료 |
| `RETRACTED` | 후진 목표 구동 시간 완료 |
| `INTERMEDIATE` | 이동 중 또는 이전 실행의 위치 미확인 |
| `UNKNOWN` | 유효한 목표/완료 정보 없음 |

리니어는 `estop`/`restart`의 영향을 받지 않습니다. 최초 명령의 완료 판정용 대기 시간만 내부적으로 유지합니다. 남은 시간 표시와 `LINEAR_PROGRESS` 카운트다운은 없으며, 이미 실행 중인 목표를 다시 보내지 않습니다. 사용권이 끊겨도 진행 중인 목표의 타이머를 동결하지 않고 완료 전 새 목표를 거절합니다. 기존 PAUSED 기록은 재시작 시 UNKNOWN으로 취급합니다.

`LINEAR_DONE:TIME_BASED`는 시간 기준 완료입니다. 실제 위치·도착 센서값이 없으므로 모든 상태에 `position_measured=false`를 유지합니다. PWM 중지가 물리 즉시 정지를 보장한다고 표시하지 않습니다.

기존 `/arm3/linear_state` Int32는 완료한 목표만 2000/1015로 발행하고, 중간/미확인 상태는 -1입니다. 새 `/arm3/linear_status` String JSON에는 `phase`, `position_state`, `target_mm`, `pause_supported=false`, `position_measured`가 있습니다. 로봇 작업 중에는 별도 리니어 명령을 거절합니다.

## 관제와 알림

동일한 `host_pkg/action/Arm` 형식을 사용합니다. 원본은 `integration/ros_ws/src/host_pkg/action/Arm.action`입니다.

```text
string command
---
bool success
string message
---
string message
```

`/arm2/data`: `BUILD_A`, `BUILD_B`, `BUILD_LOAD_A`, `BUILD_LOAD_B`, `SLIDE_BUILD`.
`/arm3/data`: `LOAD_A`, `LOAD_B`, `SLIDE_LOAD`.
Action CMD는 대문자로 통일합니다. 서버는 소문자·혼합 CMD를 접수하지 않습니다. PC `final-action`은 대문자로 전송합니다. Pi 내부 `run_episode.sh`와 셸 함수는 기존 소문자 실행 명령을 유지합니다. 두 경로 모두 `EMER_STOP`, `RESTART`, `RESET`을 접수합니다. 한 번에 하나의 조립대 작업만 실행하며, 제어 명령은 활성 작업의 종료를 기다리지 않고 처리합니다. Action cancel은 거절하고 명시적 제어 명령을 사용합니다.

관제는 기존 Turtle Bot 규격과 같은 성공 여부·정상/오류 표현을 유지합니다. `Arm.action` 필드는 변경하지 않습니다.

| Action 필드 | 전송 값 | 의미 |
|---|---|---|
| Feedback.message | IDLE | 정상 대기·작업 완료 |
| Feedback.message | RUNNING | 작업 처리 중 |
| Feedback.message | PAUSED | 팔/작업 보류 중 — 리니어 정지를 의미하지 않음 |
| Feedback.message | ERROR | 오류 |
| Result.success | 0 / 1 (false / true) | 요청한 명령 이행 실패 / 성공 |
| Result.message | IDLE / ERROR | 정상 수행 / 오류·미완료 |

Feedback은 상태가 바뀔 때만 전송하며 같은 상태를 반복하지 않습니다. 상세 지그·스텝·리니어 시작/완료·토크 이벤트는 관제 Action으로 전달하지 않습니다. Action Goal UUID가 작업을 구분합니다.

아래 상세 이벤트는 **Pi 터미널·로컬 실행 로그·기존 진단용 상태 토픽**에만 남깁니다. 관제에서 시작한 작업의 상세 출력은 `journalctl -u workcell-actions.service -f`로 볼 수 있습니다. `A_`, `B_`, `SLIDE_` 접두사를 사용합니다.

| 구간 | 이벤트 |
|---|---|
| 준비 | ACCEPTED, SAFE_POSE_STARTED, SAFE_POSE_REACHED |
| 리니어 | LINEAR_CHECK, LINEAR_READY, LINEAR_STARTED, LINEAR_CONTINUES, LINEAR_DONE:TIME_BASED |
| 지그 | JIG_DETECTION_STARTED, JIG_DETECTED, JIG_CONFIRMED, JIG_POSE |
| 계획·동작 | PLAN_STARTED, PLAN_READY, EPISODE_STARTED, STEP_DONE, STAGE_DONE, EPISODE_DONE |
| 비상정지·재개 | ESTOP_REQUESTED, PAUSED, RESUMED |
| 초기화 | RESET_STARTED, SAFE_RETURN_STARTED, SAFE_RETURN_DONE, RESET_DONE |
| 정상 마무리 | TORQUE_OFF_STARTED, TORQUE_OFF_REQUESTED, DONE (RESET은 TORQUE_OFF_CONFIRMED까지 확인) |
| 오류 | FAILED:사유, REJECTED:사유 |

조립의 STAGE_DONE은 LOWER → MIDDLE → UPPER입니다. 앱의 에피소드 조정 탭에서 지정한 완료 알림은 스텝 ID에 연결되어 이름·순서 변경에도 유지됩니다. 완료 알림 필드가 없는 기존 에피소드는 스텝 이름 규칙을 계속 사용하며, B 조립의 하단은 조정 완료 스텝까지 포함합니다. 적재는 PICK → PLACE입니다. 이는 동작 구간 완료이며 OpenCV를 통한 실제 부품 전달·조립 성공 판정은 아직 구현하지 않습니다.

일반 작업은 에피소드와 모든 검사가 끝나고 토크 해제 요청이 접수되면 Result `1 / IDLE`을 반환합니다. 5초 유지와 토크 OFF 확인 대기를 하지 않으며, 완료 결과는 6개 모터의 토크 OFF 확인을 의미하지 않습니다. 연결 정리가 끝나면 다음 명령을 받을 수 있습니다. 오류나 reset으로 취소된 원래 작업은 `0 / ERROR`입니다. EMER_STOP은 팔 정지·작업 보류 확인(PAUSED, 리니어 제외), RESTART는 재개 확인, RESET은 안전 자세 복귀·토크 OFF 완료를 확인한 뒤 해당 제어 Goal에 `1 / IDLE`을 반환합니다. 접수만 되었거나 확인에 실패한 경우 성공으로 처리하지 않습니다. 실패의 상세 사유와 RESET_DONE 같은 세부 이벤트는 Pi 터미널·로그에 남깁니다. 기존 `/arm2/episode_status`, `/arm3/episode_status`는 진단용 상세 이벤트를 유지합니다.

## 배포와 기록

### 부팅 시 네트워크 준비 확인

운영 Action 서버는 `rclpy.init()` 전에 `wlan0` 링크·사용 가능한 IPv4 주소·같은 서브넷의 연결 경로를 확인한다. 같은 상태가 2초 유지돼야 DDS 통신을 초기화한다. 특정 Wi-Fi 이름이나 관제 PC의 응답을 요구하지 않으므로 관제 PC가 꺼져 있어도 Pi는 준비할 수 있다.

주소·경로 미확정 상태가 60초 지속되면 `NETWORK_TIMEOUT` 오류로 종료한다. 기존 서비스의 `Restart=on-failure`, `RestartSec=3` 정책으로 다시 시도한다. 시작 로그에는 `NETWORK_WAIT`, `NETWORK_STATUS`, `NETWORK_READY`가 남는다. 정상 기동 뒤 발생한 네트워크 단절에 대한 자동 재시작은 하지 않으며, 작업 중 재시작을 유발하지 않는다.

Pi에서 다음 명령은 네트워크 준비만 확인하고 종료한다. ROS 노드·액션 요청·모터 연결을 만들지 않는다.

```bash
source /opt/ros/jazzy/setup.bash
source /home/ubuntu/OpenCV_teach/integration/ros_ws/install/setup.bash
python3 /home/ubuntu/OpenCV_teach/integration/action_server.py --network-check-only
```

다른 인터페이스를 쓰는 배치에서는 `WORKCELL_NETWORK_INTERFACE` 또는 `--network-interface`로 변경한다. 비구동 검증용 `--check-only` 서버는 별도 ROS 도메인에서만 허용하며 이 네트워크 대기를 생략한다. `network-online.target`만 신뢰하지 않고 프로그램 시작 시 확인하므로 기존 서비스 파일에서도 다음 시작부터 적용된다.

- 리니어: `arm3-linear-controller.service`, `/home/ubuntu/project/linear_controller_pi5.py` + `linear_motion.py`.
- 관제: `workcell-actions.service`, `/home/ubuntu/OpenCV_teach/integration/action_server.py`.
- 인터페이스: `integration/ros_ws`에서 빌드한 `host_pkg/action/Arm`.
- 설치/갱신: `sudo bash /home/ubuntu/OpenCV_teach/integration/install_services.sh`. 진행 중인 작업·최근 리니어 명령이 있으면 거절합니다.
- 에피소드: `integration/recipes.json`, Pi 각 팔의 기존 프로필·보정·에피소드 저장소 사용.
- 실행 설정: `integration/execution-settings.json` (속도 400 ticks/s, 지그 측정 5초 × 3회).
- 실행 기록: `data/episode-cli-runs/`, 마지막 상태: `data/episode-cli-arm2.json`, `data/episode-cli-arm3.json`.
- 리니어 기록: `/home/ubuntu/.local/state/arm3-linear-controller/motion.json`. 재시작 시 임의 PWM 출력이나 자동 재개를 하지 않습니다.

2026-10-07: 비구동 테스트와 별도 ROS Domain의 Action 연결을 검증했습니다. 실제 로봇 이동·리니어 구동 및 부품 처리 품질 검증은 수행하지 않았습니다. 기존 PC 수신 앱 방식은 이번 새 Action 경로와 별개이며 동시에 사용하지 않습니다.

2026-10-07 추가 변경: 검출 지그가 고정되어 있다는 사용자 확인을 바탕으로 리니어·지그 준비를 병행합니다. 리니어 완료는 기존 시간 기준이며 실제 위치 센서 판정으로 바뀐 것은 아닙니다.

지그 확정 시 `A_JIG_POSE:pallet:x_mm=123.45:y_mm=-67.89:yaw_deg=12.34` 형식으로 검출 위치·각도를 기록합니다(예시 값). `x_mm`, `y_mm`는 검출 평면 좌표이며 `yaw_deg`는 검출 각도입니다. 이는 계획 입력에 사용한 확정 검출값이며 보정 후 로봇 목표 좌표가 아닙니다. 재검출로 확정되면 새 값을 기록하고, 확정 전 후보 좌표는 확정 알림으로 내보내지 않습니다. `work_watch`와 Action 서버 로그에서 확인할 수 있습니다.

## 앱에서 A/B 실행본 갱신

PC 앱은 시작 시 팔2·팔3의 Pi 에피소드와 A/B 연결, 실행 설정을 읽어옵니다. 마지막으로 읽은 Pi 원본이 바뀌지 않았다면 같은 ID의 로컬 검토 설정을 그대로 유지합니다. Pi 원본도 변경된 경우에는 로컬 수정본을 별도 사본으로 보존한 뒤 새 Pi 원본을 불러옵니다. 연결 실패 시에는 로컬 보관본임을 표시합니다. 연결하거나 로컬 저장하는 것만으로 Pi 에피소드를 덮어쓰지 않습니다.

**티칭 → 스텝 추가·편집**에서는 자세·스텝을 편집하고, **티칭 → 에피소드 조정**에서는 완료 알림과 Pi 실행 설정을 편집합니다. **Pi 에피소드 내보내기**에서 현재 Pi A/B 연결을 조회한 뒤 선택한 에피소드를 선택한 팔의 A/B 실행에 연결합니다. 전송은 기존 SSH 설정을 사용하며 모터·카메라 연결, 서비스 재시작, 실행 명령을 호출하지 않습니다.

수신 처리는 Pi의 기존 `EpisodeStore`로 로봇팔·영점·틱·티칭 기준을 검증하고 지그 모델 호환성을 확인합니다. 팔2는 `data/episodes`, 팔3은 `data/arm3-runtime/episodes`에 새 ID의 실행본을 저장한 뒤 해당 `integration/recipes.json` 항목만 원자적으로 교체합니다. 실행기와 공유하는 `data/episode-cli.lock`을 사용하여 실행 중 갱신을 거절합니다. 새 ID를 사용하므로 이미 읽은 실행본과 다른 A/B 연결은 영향을 받지 않습니다. 실행 설정 초안을 함께 전송하면 속도·지그 측정 시간·횟수·유지 시간은 선택한 팔의 A/B 작업에 공통 적용되고, 제한시간은 선택한 A/B 항목에만 적용됩니다. 설정과 연결을 함께 검증·백업하며, 연결 저장 실패 시 기존 실행 설정을 복원합니다.

조회 당시 연결·프로필·보정·실행 설정의 지문을 적용 직전에 다시 비교합니다. 파일 저장 실패 시 기존 A/B 연결을 유지하며, 같은 전송 요청의 재확인은 중복 적용하지 않습니다. 기존 연결과 이전 에피소드의 백업은 `data/episode-transfer-backups/<전송 ID>/`입니다. 통신 실패로 완료 여부가 불명확하면 앱에서 다시 조회합니다.

### 2026-10-08 검사 실행 순서

검사 프로토콜 3은 지정 스텝 다음 스텝 완료 후 검사를 시작하고 전체 경로를 연속 실행한다. 불합격·카메라 오류·판정 시간 초과 시 즉시 팔 HOLD를 요청하고 FAILED로 종료한다. 검사 통과 전에는 연결된 단계 완료 알림과 최종 DONE을 보내지 않는다. 시작 시 리니어 이동과 지그 검출을 병행하며, 지그 검출 직후 고정 지그 시작 조건을 검사한다. 리니어 위 시작 조건은 리니어 정지 후 검사하고 모든 조건 통과까지 로봇 스텝은 대기한다. 저장된 검사 지정과 4초·5회/120초 설정은 그대로 사용한다.

### 팔로워 재연결 준비

작업 준비 시 종료된 이전 세션은 새 연결의 실패로 판단하지 않는다. `FOLLOWER_CONNECTING` 후 설정·연결을 요청하고, 새 연결의 최신 보정 일치 상태를 최대 10초 대기한다. 확인되면 `FOLLOWER_READY`를 기록하고 작업 준비를 이어간다. 새 연결 오류·시간 초과는 실패로 처리하고, 연결 준비가 끝난 이후의 연결 종료 검사는 그대로 적용한다.

### 동시 안착 검사와 알림 이름

검사 프로토콜 4는 검사 가능한 시작 조건을 한 영상에서 함께 계산하고 부품별 최소 판정 횟수(기본 2회)를 따로 누적한다. 미판정은 횟수에 포함하지 않으며 확정한 대상은 다음 계산에서 제외한다. `INSPECTION_GROUP_STARTED`와 `INSPECTION_COUNT`로 진행을 표시한다. 리니어 이동 중에는 고정 지그부터 함께 검사하고, 리니어 위 검사는 정지 후 진행한다. `work_watch`는 지그·스텝의 내부 ID를 등록 이름으로 바꾸어 보여주고 원본 이력은 유지한다.

### 조립 후 운송 연속 실행

`build_load_a` 또는 `build_load_b`는 arm2 조립을 정상 완료한 뒤 같은 제품의 arm3 운송을 이어서 실행합니다. 두 에피소드를 시작 전에 검증하고 두 단계가 끝날 때까지 작업 사용권을 유지합니다. 조립 실패·reset 시 운송을 시작하지 않고, estop 상태에서는 restart 전까지 진행하지 않습니다. ROS에서는 `/arm2/data`로 같은 명령을 보냅니다. 조립만 끝났을 때 전체 완료를 알리지 않으며 운송까지 끝나야 `A_CHAIN_DONE` 또는 `B_CHAIN_DONE`과 성공 결과를 반환합니다. 기존 터미널에는 새 셸 함수를 불러오거나 새 터미널을 열어 사용합니다.

`work_watch`의 RESET 표시는 전체 요청, 팔별 안전 복귀 시작·완료, 전체 완료로 요약합니다. 상세 진행과 중복 원문 응답은 기록에 보존하며 `work_watch --details`로 볼 수 있습니다. 오류는 기본 화면에도 표시합니다.
검사·작업 실패는 제목 아래 상세 항목을 들여써 표시합니다. 같은 실행에서 연달아 발생한 동일 검사 실패·작업 실패는 상세 원문을 한 번만 표시하고, 후속 실패에는 앞선 원인을 참조합니다. 원본 기록은 변경하지 않습니다.
기본 조회는 명령 접수, 지그 확인, 검사 결과, 주요 단계, 정지·재개와 최종 결과를 표시합니다. 개별 스텝, 검사 관측 횟수, 연결·경로 준비의 중간 알림, 지그 좌표와 정상 응답 중복은 `--details`에서 확인합니다. 리니어 완료는 구동 시간 기준이며 위치 실측이 아님을 화면에 명시합니다. 알 수 없는 새 알림·오류·거절은 숨기지 않습니다.
