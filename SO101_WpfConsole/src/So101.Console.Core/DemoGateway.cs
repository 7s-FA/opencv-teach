namespace So101.Console.Core;

/// <summary>In-memory deterministic simulator. No sockets, ROS, subprocesses or device APIs.</summary>
public sealed class DemoGateway : IControlGateway, IDemoControls
{
    private readonly Dictionary<string, RequestRecord> records = [];
    private readonly Dictionary<string, string> jobIds = [];
    private readonly Dictionary<string, string> controlIds = [];
    private string activeArm = "arm2";
    private string? activeJob;
    private int ticks;
    private bool online = true, closed, failNext, activeFailure, loseNextReply;
    private long sequence;
    public SessionInfo Session { get; private set; } = new(Protocol.Version, Protocol.NewId(), Protocol.NewId(), true);
    public event Action<GatewayEvent>? Changed;
    public int Executions { get; private set; }
    public bool HasRunningJob => activeJob is not null && records[activeJob].State is RequestState.RUNNING or RequestState.BUILD_DONE;
    public Snapshot GetSnapshot() => new(Protocol.Version, Session.BridgeInstanceId, Session.SessionGeneration, sequence,
        true, online ? ConnectionState.ONLINE : ConnectionState.COMM_LOST, DateTimeOffset.UtcNow,
        new[] { "arm2", "arm3" }.Select(arm => new ArmSnapshot(arm,
            online ? ConnectionState.ONLINE : ConnectionState.COMM_LOST,
            Lookup(jobIds.GetValueOrDefault(arm)), Lookup(controlIds.GetValueOrDefault(arm)))).ToArray());
    private RequestRecord? Lookup(string? id) => id is null ? null : records.GetValueOrDefault(id);
    private CommandReply Reject(string code, string detail) => new(Protocol.Version, false, code, detail, null);

    public string? UnavailableReason(string command)
    {
        var def = CommandCatalog.Find(command);
        if (def is null) return "알 수 없는 명령입니다.";
        if (closed) return "관제 세션이 종료됐습니다.";
        if (def.Kind == CommandKind.LOCAL) return null;
        if (!online) return "데모 연결이 끊겼습니다. 재연결 후 조회하세요.";
        if (def.Kind == CommandKind.JOB && activeJob is not null) return "진행·보류 중인 업무의 최종 결과를 먼저 확인하세요.";
        if (def.Code == "RESTART" && (activeJob is null || records[activeJob].State != RequestState.PAUSED)) return "재개할 보류 작업이 없습니다.";
        if (def.Code == "CLEAR_ERROR" && HasRunningJob) return "먼저 팔 정지·보류를 요청하세요.";
        if (def.Kind == CommandKind.QUERY && Latest(def.Code == "RETRY_CONTROL") is null) return "조회할 요청 기록이 없습니다.";
        return null;
    }

    public CommandReply Submit(CommandRequest request)
    {
        if (request.ProtocolVersion != Protocol.Version) return Reject("VERSION_MISMATCH", "지원하지 않는 통신 규약입니다.");
        if (closed || request.SessionGeneration != Session.SessionGeneration) return Reject("SESSION_EXPIRED", "만료된 관제 세션입니다.");
        if (!Protocol.ValidId(request.RequestId)) return Reject("INVALID_REQUEST", "요청 ID 형식 오류입니다.");
        var def = CommandCatalog.All.FirstOrDefault(c => c.Code == request.Command);
        if (def is null || def.Kind is CommandKind.LOCAL or CommandKind.QUERY) return Reject("INVALID_COMMAND", "실행 API에는 실행·제어 명령만 전달합니다.");
        if (records.TryGetValue(request.RequestId, out var previous))
            return previous.Command == def.Code
                ? new(Protocol.Version, true, "EXISTING_REQUEST", "기존 요청 상태 · 재실행 없음", previous)
                : Reject("ID_CONFLICT", "동일 요청 ID에 다른 명령을 사용할 수 없습니다.");
        var reason = UnavailableReason(def.Code);
        if (reason is not null) return Reject("NOT_ALLOWED", reason);
        var arm = def.Arm ?? activeArm;
        var record = new RequestRecord(request.RequestId, def.Code, arm, def.Kind,
            RequestState.ACCEPTED, "데모 요청 접수 · 완료 응답 대기", DateTimeOffset.UtcNow);
        records.Add(record.RequestId, record);
        if (def.Kind == CommandKind.JOB)
        {
            activeJob = record.RequestId; activeArm = arm; ticks = 0; activeFailure = failNext; failNext = false;
            jobIds[arm] = record.RequestId; Executions++;
            Update(record.RequestId, RequestState.RUNNING, "데모 작업 진행 중");
        }
        else
        {
            controlIds[arm] = record.RequestId;
            switch (def.Code)
            {
                case "EMER_STOP":
                    if (activeJob is not null) Update(activeJob, RequestState.PAUSED, "데모 팔 보류 · 리니어 중간 정지는 모델링하지 않음");
                    break;
                case "RESTART":
                    if (activeJob is not null) Update(activeJob, RequestState.RUNNING, "사용자 요청으로 데모 재개");
                    break;
                case "RESET":
                case "CLEAR_ERROR":
                    if (activeJob is not null)
                    {
                        Update(activeJob, def.Code == "RESET" ? RequestState.CANCELLED : RequestState.CLEARED,
                            def.Code == "RESET" ? "데모 작업 취소" : "운영자에 의해 정리됨 · 성공 완료 아님");
                        activeJob = null;
                    }
                    break;
            }
            Update(record.RequestId, RequestState.SUCCEEDED,
                def.Code == "RESET" ? "데모 복귀·토크 OFF 결과 · 실제 장치 동작 없음" : "데모 제어 결과 확인");
        }
        Publish("state_changed");
        if (loseNextReply && def.Kind == CommandKind.JOB)
        {
            loseNextReply = false;
            return new(Protocol.Version, false, "RESPONSE_UNKNOWN", "접수 응답 유실 모의 · 같은 ID로 조회하세요.",
                record with { State = RequestState.UNKNOWN });
        }
        return new(Protocol.Version, true, "ACCEPTED", "접수 응답 · 최종 결과는 별도 확인", records[record.RequestId]);
    }

    public RequestRecord? Latest(bool control) => Lookup((control ? controlIds : jobIds).GetValueOrDefault(activeArm));
    public Task<CommandReply> SubmitAsync(CommandRequest request) => Task.FromResult(Submit(request));
    public Task<RequestRecord?> QueryAsync(string requestId) => Task.FromResult(Query(requestId));
    public RequestRecord? Query(string requestId) => online && !closed ? records.GetValueOrDefault(requestId) : null;
    public void FailNextJob() => failNext = true;
    public void LoseNextReply() => loseNextReply = true;
    public void Advance()
    {
        if (!online || !HasRunningJob || activeJob is null) return;
        ticks++;
        var record = records[activeJob];
        if (ticks == 2 && (record.Command.StartsWith("BUILD_LOAD_", StringComparison.Ordinal) || record.Command.StartsWith("PC_BUILD_LOAD_", StringComparison.Ordinal)))
            Update(activeJob, RequestState.BUILD_DONE, "조립 단계 완료 · 적재 진행 중 · 전체 완료 아님");
        if (ticks >= 4)
        {
            Update(activeJob, activeFailure ? RequestState.FAILED : RequestState.SUCCEEDED,
                activeFailure ? "데모 검사 오류 · 실제 검사 결과 아님" : "데모 최종 결과 확인");
            activeJob = null;
        }
        Publish("state_changed");
    }
    public void Disconnect()
    {
        if (closed || !online) return;
        online = false;
        if (activeJob is not null) Update(activeJob, RequestState.PAUSED, "연결 단절로 데모 보류 · 재연결만으로 재개하지 않음");
        Publish("connection_changed");
    }
    public void Reconnect()
    {
        if (closed || online) return;
        Session = Session with { SessionGeneration = Protocol.NewId() };
        online = true; Publish("snapshot");
    }
    private void Update(string id, RequestState state, string detail) => records[id] = records[id] with
        { State = state, Detail = detail, UpdatedAt = DateTimeOffset.UtcNow, ResultCode = state switch
          { RequestState.SUCCEEDED => "IDLE", RequestState.FAILED or RequestState.CANCELLED => "ERROR", _ => state.ToString() } };
    private void Publish(string type)
    {
        sequence++;
        Changed?.Invoke(new(Protocol.Version, Session.BridgeInstanceId, Session.SessionGeneration, sequence,
            type, DateTimeOffset.UtcNow, GetSnapshot()));
    }
    public void Close()
    {
        if (closed) return;
        Disconnect(); closed = true;
    }
    public void Dispose() => Close();
}
