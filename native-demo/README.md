# 원본 앱 브라우저 실행

Python/Tk 앱은 서버에서 그대로 실행하고 noVNC는 화면·입력만 전달합니다. JavaScript로 티칭 UI나 로봇 계산을 다시 구현하지 않습니다.

## Codespaces

저장소의 **Open in GitHub Codespaces** 버튼으로 개인 Codespace를 만든 뒤 준비가 끝나면 Ports의 **6080**을 엽니다. 기본 포트 공개 범위는 변경하지 않습니다. 각 사용자가 자신의 Codespace와 복사된 데이터를 사용합니다. 최초 설치는 몇 분 걸리며 재시작 때는 설치된 환경을 재사용합니다.

실행은 `postStartCommand`로 시작합니다. 실행 로그는 `~/.cache/so101-native-demo/server.log`, 앱 로그는 그 아래 `session/runtime.log`에 있습니다. 종료는 Codespace를 중지하면 됩니다. 저장공간 사용도 멈추려면 필요 없는 Codespace를 삭제해야 합니다.

## 원본과 달라지는 입력

- GUI, 관절·지그 계산, 3D 렌더러, OpenCV 검출과 검사 코드는 원본 모듈을 직접 import합니다.
- 모터 통신은 원본의 `DemoSession`으로 바꿉니다. 실제 장치 연결·물리적 성공을 검증하는 데모가 아닙니다.
- 카메라는 `tests/fixtures/inspection-all/faults.jpg`를 10 FPS 이하로 공급합니다. 원본 `CameraSession.process_frames`와 `MultiDetector.process`를 그대로 사용합니다.
- 사진의 처리 결과를 정상으로 강제하거나 검출 좌표를 만들어 넣지 않습니다. 기준 위치를 찾지 못하면 원본 앱의 대기·실패 상태가 표시됩니다.
- 시작할 때 예제 설정·보정·에피소드·모델을 작업용 폴더로 복사합니다. 해당 실행의 편집은 이 복사본에만 저장됩니다. 서버를 다시 시작하면 기본 예제로 초기화합니다.
- `source-manifest.json`은 이번 원본 코드 묶음의 해시입니다. 코드 업데이트 때 갱신합니다.

## 로컬 개발

Ubuntu에서 `bash native-demo/setup.sh` 후 `bash native-demo/start.sh`를 실행합니다. 설치 스크립트는 sudo로 Xvfb·x11vnc·Tk·Mesa·한국어 글꼴을 설치하므로 시스템 패키지 설치 권한이 필요합니다. Python 패키지는 프로젝트의 `.native-venv`에만 설치합니다.

이미 준비된 환경은 `SO101_DEMO_PYTHON`, `XVFB_BIN`, `X11VNC_BIN`으로 지정할 수 있습니다. 실제 장치가 있는 PC에서는 `SO101_LOCAL_SANDBOX=1`과 bubblewrap을 사용해 앱의 네트워크·장치를 분리합니다. 로컬 검증은 이 격리 모드로 수행합니다. Codespace에는 실제 장치를 전달하지 않습니다.

서버는 `127.0.0.1:6080`에만 바인딩합니다. 웹소켓은 같은 호스트 및 해당 Codespace의 전달 주소만 허용합니다. 원시 VNC 포트를 외부로 열지 않습니다. 공개 공유 서버 용도로 포트 공개 범위를 임의 변경하지 마세요.

## 사용 라이브러리

- noVNC 1.7.0: 원본 화면 전달, npm 잠금 파일 포함, MPL-2.0
- aiohttp: 정적 파일과 WebSocket ↔ VNC 전송
- Xvfb / x11vnc: 독립 X 화면과 VNC 서버

원본 앱 시작과 3D 준비 상태는 `native-demo/check.py`로 확인합니다. 네트워크 차단과 실제 로봇 제어는 별개이며, Codespace에서도 Pi 주소나 실제 장치 설정을 넣을 필요가 없습니다.
