# SO-101 Windows 관제 소스

Windows WPF 화면·명령 카탈로그·연결 상태·중복 요청 방지와 데모/실장치 게이트웨이 소스다. Ubuntu ROS 중계는 형제 폴더 `SO101_PC_Package`의 `bridge_main.py`를 사용한다. 데모 모드와 실제 연결을 구분하며 실제 연결은 별도 인증 프로필을 필요로 한다.

## 개발

`global.json`의 .NET SDK와 Windows 빌드 환경을 사용한다.

```powershell
dotnet test tests/So101.Console.Tests/So101.Console.Tests.csproj
dotnet build src/So101.Console.Wpf/So101.Console.Wpf.csproj -c Release
```

프로토콜은 `contracts/openapi.json`, `contracts/protocol.schema.json`, `contracts/websocket.md`에 있다. `connection.example.json`은 자리표시자이며 실제 인증 프로필이 아니다. 테스트는 가상 게이트웨이와 로컬 전송을 사용한다.

개인 VM 실행기·설정, 인증 프로필, 설치된 실행 파일과 SDK 캐시는 포함하지 않는다. 이 소스 갱신은 새 Windows 설치본이나 GitHub 릴리스 배포를 의미하지 않는다. 작업대 적용·검증 범위는 [현재 요약](../docs/progress-2026-10-29.md)을 참고한다.
