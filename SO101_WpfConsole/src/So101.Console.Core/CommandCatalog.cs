namespace So101.Console.Core;

public enum CommandKind { JOB, CONTROL, QUERY, LOCAL }
public sealed record CommandDefinition(string Code, string Label, string Group, string Description,
    CommandKind Kind, string? Arm = null);

public static class CommandCatalog
{
    public static readonly IReadOnlyList<CommandDefinition> All = [
        new("BUILD_A", "A 조립", "조립·적재", "팔2 A 조립", CommandKind.JOB, "arm2"),
        new("LOAD_A", "A 적재", "조립·적재", "팔3 A 적재", CommandKind.JOB, "arm3"),
        new("BUILD_B", "B 조립", "조립·적재", "팔2 B 조립", CommandKind.JOB, "arm2"),
        new("LOAD_B", "B 적재", "조립·적재", "팔3 B 적재", CommandKind.JOB, "arm3"),
        new("BUILD_LOAD_A", "A 조립 → 적재", "조립·적재", "팔2 조립 후 팔3 적재 · 최종 결과까지 대기", CommandKind.JOB, "arm2"),
        new("BUILD_LOAD_B", "B 조립 → 적재", "조립·적재", "팔2 조립 후 팔3 적재 · 최종 결과까지 대기", CommandKind.JOB, "arm2"),
        new("PC_BUILD_LOAD_A", "PC · A 조립 → 적재", "PC 협력", "PC GPU·CAD 판정 + Pi 실행 · 저장 영상 검증 후 활성화", CommandKind.JOB, "arm2"),
        new("PC_BUILD_LOAD_B", "PC · B 조립 → 적재", "PC 협력", "PC GPU·CAD 판정 + Pi 실행 · 저장 영상 검증 후 활성화", CommandKind.JOB, "arm2"),
        new("BUILD_SLIDE", "리니어 조립 위치", "리니어·복귀", "양팔 토크·안전자세·카메라·모델 준비 + 리니어 조립 위치 · 완료 후 3분 준비 유지", CommandKind.JOB, "arm2"),
        new("LOAD_SLIDE", "리니어 하차 위치", "리니어·복귀", "양팔 토크·안전자세·카메라·모델 준비 + 리니어 하차 위치 · 완료 후 3분 준비 유지", CommandKind.JOB, "arm3"),
        new("PREPARE_HOME", "안전 자세 복귀", "리니어·복귀", "두 팔 안전 복귀 → 리니어 하차 → 카메라·모델 준비 · 완료 후 3분 준비 유지", CommandKind.JOB, "arm2"),
        new("EMER_STOP", "팔 정지·보류", "제어", "기존 대상 팔의 정지·작업 보류 · 리니어는 계속 진행", CommandKind.CONTROL),
        new("RESTART", "작업 재개", "제어", "보류 작업의 자세·지그·새 영상 재검사 통과 후 이어서 실행", CommandKind.CONTROL),
        new("RESET", "취소·복귀·OFF", "제어", "작업 취소 · 두 팔 순차 안전 복귀 · 토크 OFF", CommandKind.CONTROL),
        new("CLEAR_ERROR", "보류 종료·오류 해제", "제어", "이동·토크 변경 없음 · 실행부 조건 확인 필요", CommandKind.CONTROL),
        new("RETRY", "업무 결과 조회", "조회·앱", "기존 업무 ID 조회 · 재실행 없음", CommandKind.QUERY),
        new("RETRY_CONTROL", "제어 결과 조회", "조회·앱", "기존 제어 요청 ID 조회", CommandKind.QUERY),
        new("STATUS", "연결·작업 상태", "조회·앱", "양팔의 연결과 현재 요청 상태 확인", CommandKind.LOCAL),
        new("HELP", "전체 명령 설명", "조회·앱", "명령 설명과 사용 방법 확인", CommandKind.LOCAL),
        new("QUIT", "관제 종료", "조회·앱", "관제 세션 종료 · 자동 복귀 명령 없음", CommandKind.LOCAL)
    ];
    public static string Normalize(string value) => value.Trim().ToUpperInvariant() switch
    {
        "1" => "BUILD_A", "2" => "LOAD_A", "3" => "BUILD_B", "4" => "LOAD_B",
        "5" => "BUILD_SLIDE", "6" => "LOAD_SLIDE", "ESTOP" => "EMER_STOP",
        "Q" or "EXIT" => "QUIT", var code => code
    };
    public static CommandDefinition? Find(string value) => All.FirstOrDefault(c => c.Code == Normalize(value));
}
