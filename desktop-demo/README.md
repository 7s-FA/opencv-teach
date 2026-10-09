# SO-101 Teach 다운로드 데모

원본 Python/Tk GUI, 3D 작업대와 로봇팔, 티칭 편집·저장, OpenCV 지그 검출을 포함한 오프라인 데모입니다. Python을 별도로 설치하지 않아도 됩니다. 실제 모터·카메라·Pi에는 연결하지 않습니다. 3D 표시에는 OpenGL을 지원하는 그래픽 드라이버가 필요합니다.

## 실행

- **Windows 10/11, 64비트:** ZIP 전체를 압축 해제한 뒤 `SO101-Teach-Demo.exe`를 실행합니다. `_internal` 폴더를 함께 보관하세요. 코드 서명 인증서는 적용하지 않았으므로 Windows가 게시자를 확인할 수 없다는 안내를 표시할 수 있습니다.
- **Ubuntu 22.04/24.04, x86-64:** `tar.gz`를 압축 해제한 뒤 `SO101-Teach-Demo` 실행 파일을 엽니다. 터미널에서는 `./SO101-Teach-Demo`를 실행합니다. 그래픽 데스크톱과 OpenGL 드라이버가 필요합니다. 한글 글꼴이 없다면 `sudo apt install fonts-noto-cjk`로 설치합니다. 다른 Linux 배포판은 별도 검증하지 않았습니다.

앱에서 로봇팔2·3을 선택하고 저장된 에피소드와 스텝을 편집할 수 있습니다. 카메라 화면은 기본적으로 **지그 2개가 검출되는 저장 사진**을 원본 검출기에 반복 입력합니다. 상단 **데모 사진** 메뉴에서 조립 완료·오안착 사진으로 바꿀 수 있습니다. 사진을 바꾸면 위치 측정을 다시 시작합니다. 검출 성공 여부를 강제로 바꾸지 않습니다.

모터의 위치와 이동은 원본 `DemoSession`이 처리하는 가상 동작입니다. 데모에서 저장한 설정은 실물 실행 검증을 거친 데이터가 아닙니다. 실제 장치 연결·ROS·Pi 전송 기능은 이 배포판에서 차단합니다.

## 저장 위치

- Windows: `%LOCALAPPDATA%\SO101TeachDemo\v1`
- Linux: `~/.local/share/SO101TeachDemo/v1` (`XDG_DATA_HOME`을 설정했다면 그 아래)

편집한 에피소드는 다시 실행해도 유지됩니다. 처음 상태로 되돌리려면 앱을 종료하고 해당 폴더를 다른 이름으로 옮긴 뒤 다시 실행하세요. 기존 실사용 `SO101_teach/data`는 사용하지 않습니다. 시작 오류는 같은 폴더의 `startup-error.log`에 기록됩니다.

## 개발·검증

Python 3.12 환경에서 `pip install -r desktop-demo/requirements-build.txt`, `pyinstaller --clean --noconfirm desktop-demo/demo.spec`로 빌드합니다. 각 OS에서 별도로 빌드해야 합니다. 생성된 실행 파일의 `--smoke-test report.json --data-dir 임시폴더` 옵션은 실제 Tk GUI·양쪽 팔 3D·지그 2개 검출·사진 변경·저장 유지·실장치 연결 차단을 검증하고 종료합니다. Windows CI의 GPU 없는 가상 머신에서는 테스트할 때만 Mesa 소프트웨어 렌더러를 사용하며 배포 파일에는 포함하지 않습니다.

포함된 외부 라이브러리와 모델의 라이선스는 `third-party-licenses/`를 참고하세요. 프로젝트 소유 코드에 새 라이선스를 부여하지 않습니다.
