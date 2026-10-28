using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace So101.Console.Core;

public sealed class CommandButtonViewModel(CommandDefinition definition, Func<string, Task> execute) : ObservableObject
{
    public CommandDefinition Definition { get; } = definition;
    public AsyncRelayCommand Invoke { get; } = new(() => execute(definition.Code));
    private string? reason;
    public string? Reason { get => reason; private set { SetProperty(ref reason, value); OnPropertyChanged(nameof(Available)); OnPropertyChanged(nameof(Hint)); } }
    public bool Available => Reason is null;
    public string Hint => Reason ?? Definition.Description;
    public void Refresh(IControlGateway gateway) => Reason = gateway.UnavailableReason(Definition.Code);
    public override string ToString() => Definition.Label;
}
public sealed record CommandGroup(string Name, IReadOnlyList<CommandButtonViewModel> Commands) { public override string ToString() => Name; }
public sealed class HistoryRow(string command) : ObservableObject
{
    public string Command { get; } = command;
    public string Time { get; } = DateTimeOffset.Now.ToString("HH:mm:ss");
    private string text = "입력 접수", requestId = "—", state = "입력 접수";
    public string Text { get => text; set => SetProperty(ref text, value); }
    public string State { get => state; set => SetProperty(ref state, value); }
    public string RequestId { get => requestId; set => SetProperty(ref requestId, value); }
    public bool AwaitingQuery { get; set; }
    public bool Terminal { get; set; }
    public override string ToString() => Command;
}
public sealed class ArmViewModel(string name) : ObservableObject
{
    public string Name { get; } = name;
    private string connection = "연결 확인 중", job = "업무 없음", control = "제어 요청 없음", detail = "수신된 업무 정보가 없습니다.", tone = "neutral", connectionTone = "offline";
    public string Detail { get => detail; set => SetProperty(ref detail, value); }
    public string Tone { get => tone; set => SetProperty(ref tone, value); }
    public string ConnectionTone { get => connectionTone; set => SetProperty(ref connectionTone, value); }
    public string Connection { get => connection; set => SetProperty(ref connection, value); }
    public string Job { get => job; set => SetProperty(ref job, value); }
    public string Control { get => control; set => SetProperty(ref control, value); }
}

public sealed class MainViewModel : ObservableObject, IDisposable
{
    private readonly IControlGateway gateway;
    public ObservableCollection<HistoryRow> History { get; } = [];
    public IReadOnlyList<CommandGroup> Groups { get; }
    public ArmViewModel Arm2 { get; } = new("팔2 · 조립");
    public ArmViewModel Arm3 { get; } = new("팔3 · 적재");
    public AsyncRelayCommand SubmitInput { get; }
    public RelayCommand AdvanceDemo { get; }
    public RelayCommand DisconnectDemo { get; }
    public RelayCommand ReconnectDemo { get; }
    public RelayCommand FailNextDemo { get; }
    public RelayCommand LoseReplyDemo { get; }
    public RelayCommand CloseHelp { get; }
    public AsyncRelayCommand Reconnect { get; }
    public bool IsDemo => gateway is IDemoControls;
    public bool IsLive => !IsDemo;
    public string ModeTitle => IsDemo ? "DEMO / 장치 연결 없음" : "LIVE / 수동 관제";
    public string ModeDescription => IsDemo ? "장치를 움직이지 않는 모의 운전" : "작업 명령은 실제 장치로 전달됩니다";
    public string WindowTitle => IsDemo ? "SO-101 · 데모 관제" : "SO-101 · 작업 관제";
    public string BridgeLabel => IsDemo ? "중계 · 모의 연결" : "Ubuntu 중계";
    public event Action? CloseRequested;
    private string input = "", notice = "명령을 선택하세요. 모든 동작은 데모이며 장치를 움직이지 않습니다.", connection = "데모 연결 정상", updated = "—", target = "팔2";
    private bool helpVisible;
    private string systemState = "연결 확인 중", systemTone = "neutral";
    public string SystemState { get => systemState; private set => SetProperty(ref systemState, value); }
    public string SystemTone { get => systemTone; private set => SetProperty(ref systemTone, value); }
    public string Input { get => input; set => SetProperty(ref input, value); }
    public string Notice { get => notice; set => SetProperty(ref notice, value); }
    public string Connection { get => connection; private set => SetProperty(ref connection, value); }
    public string Updated { get => updated; private set => SetProperty(ref updated, value); }
    public string Target { get => target; private set => SetProperty(ref target, value); }
    public bool HelpVisible { get => helpVisible; set { SetProperty(ref helpVisible, value); OnPropertyChanged(nameof(MainEnabled)); } }
    public bool MainEnabled => !HelpVisible;
    public CommandButtonViewModel EmergencyStop => Groups.SelectMany(g => g.Commands).Single(c => c.Definition.Code == "EMER_STOP");
    public string HelpText => string.Join("\n\n", CommandCatalog.All.Select(c => $"{c.Code}  ·  {c.Label}\n{c.Description}")) +
        "\n\n숫자 1~6과 estop 별칭을 지원합니다.\nF1 도움말 · Esc 입력창 · Enter 명령 전송\n" +
        (IsDemo ? "데모 상태는 종료하면 초기화됩니다." : "통신 복구 시 자동 실행하지 않습니다. 미확정 요청은 RETRY로 같은 ID를 조회하세요.\n관제 종료 시 담당 작업은 생존 신호 만료 후 보류됩니다. 팔 정지·보류는 리니어 중간 정지가 아닙니다.");

    public MainViewModel(IControlGateway gateway, IDemoControls? demoControls = null)
    {
        this.gateway = gateway;
        Groups = CommandCatalog.All.GroupBy(c => c.Group).Select(g => new CommandGroup(g.Key,
            g.Select(c => new CommandButtonViewModel(c, ExecuteAsync)).ToArray())).ToArray();
        SubmitInput = new(async () => { var value = Input; Input = ""; await ExecuteAsync(value); });
        AdvanceDemo = new(() => demoControls?.Advance());
        DisconnectDemo = new(() => demoControls?.Disconnect());
        ReconnectDemo = new(() => demoControls?.Reconnect());
        FailNextDemo = new(() => { demoControls?.FailNextJob(); Notice = "다음 데모 작업의 최종 결과를 오류로 표시합니다."; });
        LoseReplyDemo = new(() => { demoControls?.LoseNextReply(); Notice = "다음 업무의 접수 응답을 유실시킵니다. RETRY로 같은 ID를 조회하세요."; });
        CloseHelp = new(() => HelpVisible = false);
        Reconnect = new(async () => { if (gateway is ILiveControls live) await live.ReconnectAsync(); });
        if (!IsDemo) Notice = "연결 상태를 확인한 뒤 작업을 선택하세요.";
        gateway.Changed += OnChanged;
        Refresh(gateway.GetSnapshot());
    }
    public async Task ExecuteAsync(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return;
        var code = CommandCatalog.Normalize(raw);
        var row = new HistoryRow(code);
        History.Insert(0, row);
        while (History.Count > 2) History.RemoveAt(History.Count - 1);
        OnPropertyChanged(nameof(HistoryEmpty));
        var def = CommandCatalog.Find(code);
        if (def is null) { row.State = "입력 오류"; row.Text = "알 수 없는 명령입니다. HELP에서 목록을 확인하세요."; return; }
        if (def.Kind == CommandKind.LOCAL)
        {
            row.State = "확인 완료";
            row.Text = code switch { "STATUS" => IsDemo ? "현재 데모 연결·업무 상태를 조회했습니다." : "수신 중인 연결·업무 상태를 확인했습니다.", "HELP" => "전체 명령 설명을 표시합니다.", _ => "관제 세션을 종료합니다." };
            if (code == "HELP") HelpVisible = true;
            if (code == "QUIT") { gateway.Close(); CloseRequested?.Invoke(); }
            Refresh(gateway.GetSnapshot()); return;
        }
        if (def.Kind == CommandKind.QUERY)
        {
            var prior = gateway.Latest(code == "RETRY_CONTROL");
            var result = prior is null ? null : await gateway.QueryAsync(prior.RequestId);
            if (result is null) { row.State = "조회 불가"; row.Text = gateway.UnavailableReason(code) ?? "조회할 요청이 없습니다."; return; }
            foreach (var old in History.Where(h => h.RequestId == result.RequestId)) old.AwaitingQuery = false;
            Apply(row, result); Refresh(gateway.GetSnapshot()); return;
        }
        var request = new CommandRequest(Protocol.Version, gateway.Session.SessionGeneration, Protocol.NewId(), code);
        row.RequestId = request.RequestId;
        var reply = await gateway.SubmitAsync(request);
        if (reply.Request is not null) Apply(row, reply.Request);
        else { row.State = "접수 거절"; row.Text = reply.Detail; }
        if (reply.Code == "RESPONSE_UNKNOWN") { row.AwaitingQuery = true; row.State = "결과 미확인"; row.Text = reply.Detail; }
        Refresh(gateway.GetSnapshot());
    }
    private void OnChanged(GatewayEvent value) => Refresh(value.Snapshot);
    private static void Apply(HistoryRow row, RequestRecord record)
    {
        row.RequestId = record.RequestId; row.State = StateLabel(record.State); row.Terminal = record.Terminal;
        row.Text = $"{(record.Arm == "arm2" ? "팔2" : "팔3")} · {record.Detail}";
    }
    private void Refresh(Snapshot snapshot)
    {
        var online = snapshot.BridgeConnection == ConnectionState.ONLINE;
        var jobs = snapshot.Arms.Select(a => a.Job).OfType<RequestRecord>().ToArray();
        var controls = snapshot.Arms.Select(a => a.Control).OfType<RequestRecord>().ToArray();
        (SystemState, SystemTone) = !online ? ("중계 연결 끊김 · 명령 제한", "error") :
            snapshot.StorageError is not null ? ("기록 저장 오류 · 일반 실행 차단", "error") :
            snapshot.Arms.Any(a => a.Connection != ConnectionState.ONLINE) ? ("실행부 연결 확인 필요", "warning") :
            controls.Any(c => c.State == RequestState.UNKNOWN) ? ("제어 결과 미확인 · 기존 요청 조회 필요", "warning") :
            controls.Any(c => !c.Terminal) ? ("제어 요청 결과 대기", "warning") :
            jobs.Any(j => j.State == RequestState.PAUSED) ? ("작업 보류 · 재개 명령 대기", "warning") :
            jobs.Any(j => j.State == RequestState.UNKNOWN) ? ("결과 미확인 · 기존 요청 조회 필요", "warning") :
            jobs.Any(j => j.State is RequestState.RUNNING or RequestState.BUILD_DONE) ? ("작업 진행 중 · 최종 결과 대기", "running") :
            jobs.Any(j => j.State == RequestState.ACCEPTED) ? ("요청 접수 · 실행부 결과 대기", "neutral") :
            jobs.Any(j => j.State == RequestState.FAILED) ? ("최근 작업 실패 · 결과 확인 필요", "error") : controls.Any(c => c.State == RequestState.FAILED) ? ("최근 제어 실패 · 결과 확인 필요", "error") :
            (IsDemo ? "데모 준비 · 장치 연결 없음" : "대기 · 양팔 연결됨", "neutral");
        Connection = online ? (IsDemo ? "데모 연결 정상" : "연결됨") : "통신 끊김 · 자동 재개 없음";
        if (gateway is ILiveControls live) Notice = live.ConnectionDetail;
        Updated = snapshot.ObservedAt == DateTimeOffset.MinValue ? "수신 대기" : snapshot.ObservedAt.ToLocalTime().ToString("HH:mm:ss");
        foreach (var arm in snapshot.Arms)
        {
            var vm = arm.Arm == "arm2" ? Arm2 : Arm3;
            var armOnline = online && arm.Connection == ConnectionState.ONLINE;
            vm.ConnectionTone = armOnline ? "online" : "offline";
            vm.Connection = armOnline ? (IsDemo ? "데모 연결됨" : "양방향 연결됨") : "통신 끊김 · 마지막 상태";
            vm.Tone = !armOnline ? "offline" : arm.Job?.State switch { RequestState.PAUSED => "paused", RequestState.FAILED => "error", RequestState.UNKNOWN => "paused", RequestState.RUNNING or RequestState.BUILD_DONE => "running", _ => "online" };
            vm.Detail = arm.Job?.Detail ?? "새 작업을 기다리고 있습니다.";
            vm.Job = arm.Job is null ? "업무 없음" : $"{arm.Job.Command} · {((armOnline || arm.Job.Terminal) ? StateLabel(arm.Job.State) : "결과 미확인")}";
            vm.Control = arm.Control is null ? "제어 요청 없음" : $"{arm.Control.Command} · {((armOnline || arm.Control.Terminal) ? StateLabel(arm.Control.State) : "결과 미확인")}";
        }
        foreach (var row in History)
        {
            if (row.AwaitingQuery || !Protocol.ValidId(row.RequestId)) continue;
            var record = online ? snapshot.Arms.SelectMany(a => new[] { a.Job, a.Control }).FirstOrDefault(r => r?.RequestId == row.RequestId) : null;
            if (record is not null) Apply(row, record);
            else if (!online && !row.Terminal) { row.State = "결과 미확인"; row.Text = "연결이 끊겨 현재 결과를 확인할 수 없습니다. 재연결 후 조회하세요."; }
        }
        var last = gateway.Latest(false);
        Target = (IsDemo ? last?.Arm : snapshot.ActiveArm) == "arm3" ? "팔3" : "팔2";
        foreach (var button in Groups.SelectMany(g => g.Commands)) button.Refresh(gateway);
        OnPropertyChanged(nameof(HistoryEmpty));
    }
    public bool HistoryEmpty => History.Count == 0;
    public static string StateLabel(RequestState state) => state switch
    {
        RequestState.ACCEPTED => "접수", RequestState.RUNNING => "진행 중", RequestState.BUILD_DONE => "조립 완료 · 적재 중",
        RequestState.PAUSED => "보류", RequestState.UNKNOWN => "결과 미확인", RequestState.SUCCEEDED => "완료",
        RequestState.FAILED => "실패", RequestState.CANCELLED => "취소", RequestState.CLEARED => "정리됨", _ => state.ToString()
    };
    public void Dispose() { gateway.Changed -= OnChanged; gateway.Dispose(); }
}
