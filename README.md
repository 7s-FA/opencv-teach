# OpenCV Teach · SO-101 조립대 티칭

카메라로 지그의 위치·방향을 찾고, 저장된 티칭 기준과의 차이를 반영해 로봇팔의 집기·놓기 목표를 보정하는 프로그램입니다. 조립대의 로봇팔2·3과 리니어를 연결하는 PC 티칭 GUI와 Pi 실행 코드를 담았습니다.

**[웹 데모 열기](https://7s-fa.github.io/opencv-teach/)** · [이전 SO-101 작업](https://github.com/7s-FA/so101-workbench)

![기존 티칭 GUI 구조를 따르는 웹 데모](docs/web-demo.png)

## 핵심 흐름

1. 자세와 지그 기준을 함께 저장합니다.
2. 실행 전에 카메라로 현재 지그 위치·방향을 다시 읽습니다.
3. 저장한 지그 상대 위치를 현재 기준으로 옮겨 목표 자세를 계산합니다.
4. Pi에서 작업 스텝을 실행합니다. 부품·조립 검사는 필요한 단계에서 추가로 사용합니다.

## 구성

| 경로 | 내용 |
|---|---|
| `SO101_teach/` | 데스크톱 티칭 GUI, 지그 검출·보정, Pi 실행·ROS 2 연동 |
| `SO101_teach/examples/data/` | 오프라인 예제 설정·보정·에피소드·지그 모델 |
| `web-demo/` | 기존 GUI 구조를 따르는 브라우저 체험판 |
| `third-party-licenses/` | 사용한 외부 모델·코드의 라이선스 원문 |

실사용 주소·USB 식별자와 인증 정보는 공개 설정에서 제외했습니다. 기록된 보정값과 에피소드는 개발 당시 장비의 예제이며 다른 팔에 그대로 적용하는 실행 설정이 아닙니다.

## 데스크톱 오프라인 실행

Python 3.12와 Tk가 있는 Linux 환경을 기준으로 합니다. Ubuntu에서는 `python3-tk`가 필요합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r SO101_teach/requirements-desktop.txt
python SO101_teach/tools/init_demo.py
bash SO101_teach/run.sh --check
bash SO101_teach/run.sh --demo
```

예제 초기화는 `data/`가 이미 있으면 중단하고 기존 설정을 보존합니다. 실제 장치 연결·보정·Pi 배포는 [프로그램 설명](SO101_teach/README.md), [지그 보정](SO101_teach/JIG_FOLLOWING.md), [Pi 준비](SO101_teach/PI_SETUP.md), [통합 실행](SO101_teach/INTEGRATION.md)을 확인하세요. 로컬 환경 경로는 자신의 설치 위치로 설정해야 합니다.

## 웹 데모 실행

```bash
cd web-demo
npm ci
npm run dev
```

스텝 선택·관절 편집·복사·저장·재생, 실제 모델의 3D 표시, 저장 사진의 검출·검사 결과, 지그 좌표 변환 체험을 제공합니다. 편집 내용은 브라우저에 저장되며 초기화할 수 있습니다. 실시간 OpenCV 처리·역기구학·장비 통신·모터 구동은 웹에서 수행하지 않습니다.

## 검증

```bash
cd web-demo
npm test
npm run build
```

데스크톱 검사는 `SO101_teach/run_tests.sh`로 관련 테스트를 선택해 실행합니다. ROS 2·실물·그래픽 환경이 필요한 검사를 구분해야 합니다. 이 공개본은 2026-10-09 작업본을 정리한 것으로, 실제 장비에서 전체 회귀 검사를 새로 수행한 배포본은 아닙니다.

화면 기준은 [DESIGN.md](SO101_teach/DESIGN.md)입니다. 외부 모델은 원래 라이선스를 따릅니다. 프로젝트 전체에 별도의 재배포 라이선스를 새로 부여하지 않았습니다.
