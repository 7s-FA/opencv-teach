# SO-101 PC 액션 관제

SO-101 팔2·팔3의 ROS 2 Action 클라이언트다. `BUILD_DONE` 중간 알림과 최종 Result를 구분하고 PREPARE_HOME·CLEAR_ERROR를 지원한다. 팀의 다른 노드는 별도로 준비한다.

## 실행 환경

ROS Jazzy와 같은 정의의 `host_pkg/Arm` 인터페이스가 필요하다. 이 폴더에서 다음과 같이 구성할 수 있다. ROS 설치와 Pi 네트워크·장치 구성은 별도이며, 공개 데모는 이 관제를 자동 실행하지 않는다.

```bash
source /opt/ros/jazzy/setup.bash
python3 -m pip install -r requirements.txt
colcon build --base-paths new_package_template/host_pkg
source install/setup.bash
export ROS_DOMAIN_ID=40
python3 main.py --check-connection
python3 main.py
```

`--check-connection`은 서버 검색만 수행한다. 실제 작업 명령은 관제 입력에서 전송된다. 기존 작업대의 `final-action`은 로컬 환경 함수이며 이 저장소가 설치하는 명령은 아니다.

## 관제 명령

| 입력 | 전송 CMD | 동작 |
|---|---|---|
| `BUILD_A` | `BUILD_A` | 팔2 A 조립 |
| `LOAD_A` | `LOAD_A` | 팔3 A 적재 |
| `BUILD_B` | `BUILD_B` | 팔2 B 조립 |
| `LOAD_B` | `LOAD_B` | 팔3 B 적재 |
| `BUILD_LOAD_A` | `BUILD_LOAD_A` | A 조립 후 적재 |
| `BUILD_LOAD_B` | `BUILD_LOAD_B` | B 조립 후 적재 |
| `BUILD_SLIDE` | `BUILD_SLIDE` | 리니어 조립 위치 |
| `LOAD_SLIDE` | `LOAD_SLIDE` | 리니어 하차 위치 |
| `PREPARE_HOME` | `PREPARE_HOME` | 두 팔 안전 자세 복귀 후 리니어 하차 위치, 팔 토크 유지 |
| `EMER_STOP` | `EMER_STOP` | 팔 정지·작업 보류, 리니어는 계속 진행 |
| `RESTART` | `RESTART` | 보류한 작업 재개 |
| `RESET` | `RESET` | 작업 취소·두 팔 안전 복귀·토크 OFF |
| `CLEAR_ERROR` | `CLEAR_ERROR` | 보류 작업 종료·오류 해제, 이동·토크 변경 없음 |
| `STATUS` | 없음 | 서버 연결과 이 관제의 작업 상태 |
| `HELP` | 없음 | 명령 목록 |
| `QUIT` | 없음 | 관제만 종료, Pi 작업은 계속됨 |

도움말과 명령 표시는 Pi 프로토콜과 같은 대문자 CMD를 사용합니다. 입력은 대소문자를 구분하지 않으며 Pi에는 대문자 CMD를 보냅니다. 기존 `estop` 입력도 `EMER_STOP`으로 전송합니다. 작업 완료는 Action의 실제 Result로 판단하고, 정지·재개·초기화 요청의 결과는 원래 작업 결과와 구분합니다. 로그가 출력되어도 `명령> `와 입력 중인 문장을 유지합니다.

`BUILD_LOAD_A/B`의 `BUILD_DONE` Feedback은 조립 단계 완료를 뜻하며, 적재까지 마친 Action Result를 받아야 작업이 끝납니다. `CLEAR_ERROR`는 리니어가 완료 상태이고 보류 작업을 종료한 것을 확인해야 성공합니다. 실행 중인 작업을 재개하거나 팔을 복귀시키지 않습니다. `PREPARE_HOME`은 저장된 두 팔의 안전 자세와 정지한 리니어를 먼저 확인한 뒤 실행하며, 일반 `RESET`과 달리 팔의 토크를 유지합니다.

## 파일

- `main.py`: 관제 실행과 서버 검색
- `arm2_node.py`: `/arm2/data`, `/arm3/data` Action 클라이언트
- `control.py`: 명령 입력과 라우팅
- `console_ui.py`: 비동기 ROS 로그와 입력 프롬프트 표시
- `requirements.txt`: 터미널 입력 라이브러리
- `interfaces/Arm.action`: 현재 인터페이스 정의
- `new_package_template/host_pkg`: 다른 ROS 워크스페이스에 사용할 인터페이스 패키지

ROS 빌드 결과는 CPU·운영체제별로 생성하며 저장소에 포함하지 않는다. 위 환경은 사용자 PC에서 구성해야 한다.

이 복구본은 SO-101 팔2·팔3 관제 실행 파일을 백업에서 복원하고, 확인된 대문자 CMD·BUILD_SLIDE/LOAD_SLIDE 변경을 적용했습니다. 원래의 별도 팀 모듈은 포함하지 않습니다. `--with-team-nodes`는 해당 원본 파일을 별도로 준비한 경우에만 사용할 수 있습니다.
