using System.Text.Json;
using So101.Console.Core;
using Xunit;

namespace So101.Console.Tests;

public sealed class DemoTests
{
    private static CommandRequest Request(DemoGateway g, string code, string? id = null) =>
        new(Protocol.Version, g.Session.SessionGeneration, id ?? Protocol.NewId(), code);

    [Fact] public void StartupDoesNotExecuteAndCatalogCoversAllCommands()
    {
        using var g = new DemoGateway(); using var vm = new MainViewModel(g, g);
        Assert.Equal(20, CommandCatalog.All.Count); Assert.Equal(0, g.Executions);
        Assert.Empty(vm.History); Assert.True(vm.HistoryEmpty);
        Assert.All(g.GetSnapshot().Arms, a => Assert.Null(a.Job));
    }
    [Theory]
    [InlineData(" build_a ", "BUILD_A")][InlineData("1", "BUILD_A")][InlineData("2", "LOAD_A")]
    [InlineData("3", "BUILD_B")][InlineData("4", "LOAD_B")][InlineData("5", "BUILD_SLIDE")]
    [InlineData("6", "LOAD_SLIDE")][InlineData("estop", "EMER_STOP")][InlineData("exit", "QUIT")]
    public void ExistingAliasesArePreserved(string input, string expected) => Assert.Equal(expected, CommandCatalog.Normalize(input));

    [Theory]
    [InlineData("BUILD_A", "arm2")][InlineData("BUILD_B", "arm2")]
    [InlineData("LOAD_A", "arm3")][InlineData("LOAD_B", "arm3")]
    [InlineData("BUILD_LOAD_A", "arm2")][InlineData("BUILD_LOAD_B", "arm2")]
    [InlineData("PC_BUILD_LOAD_A", "arm2")][InlineData("PC_BUILD_LOAD_B", "arm2")]
    [InlineData("BUILD_SLIDE", "arm2")][InlineData("LOAD_SLIDE", "arm3")][InlineData("PREPARE_HOME", "arm2")]
    public void EveryJobUsesTheExistingArmRoute(string command, string arm)
    {
        using var g = new DemoGateway(); var request = Request(g, command); var reply = g.Submit(request);
        Assert.True(reply.Accepted); Assert.Equal(arm, reply.Request!.Arm);
        for (var i = 0; i < 4; i++) g.Advance();
        Assert.Equal(RequestState.SUCCEEDED, g.Query(request.RequestId)!.State);
    }

    [Fact] public void DuplicateIdNeverExecutesTwiceEvenAfterCompletion()
    {
        using var g = new DemoGateway(); var request = Request(g, "BUILD_A");
        Assert.True(g.Submit(request).Accepted); Assert.True(g.Submit(request).Accepted);
        for (var i = 0; i < 4; i++) g.Advance();
        Assert.Equal("EXISTING_REQUEST", g.Submit(request).Code); Assert.Equal(1, g.Executions);
        Assert.Equal(RequestState.SUCCEEDED, g.Query(request.RequestId)!.State);
        Assert.Equal("ID_CONFLICT", g.Submit(request with { Command = "LOAD_A" }).Code);
    }
    [Fact] public void BuildDoneIsNotTerminalAndPreventsAnotherJob()
    {
        using var g = new DemoGateway(); var request = Request(g, "BUILD_LOAD_B"); g.Submit(request);
        g.Advance(); g.Advance();
        var stage = g.Query(request.RequestId)!;
        Assert.Equal(RequestState.BUILD_DONE, stage.State); Assert.False(stage.Terminal);
        Assert.False(g.Submit(Request(g, "BUILD_A")).Accepted);
        g.Advance(); g.Advance(); Assert.Equal(RequestState.SUCCEEDED, g.Query(request.RequestId)!.State);
    }
    [Fact] public void StopResultDoesNotCompleteJobAndResumeRequiresUserCommand()
    {
        using var g = new DemoGateway(); var job = Request(g, "LOAD_B"); g.Submit(job);
        var stop = g.Submit(Request(g, "EMER_STOP"));
        Assert.Equal(RequestState.SUCCEEDED, stop.Request!.State);
        Assert.Equal("arm3", stop.Request.Arm);
        for (var i = 0; i < 5; i++) g.Advance();
        Assert.Equal(RequestState.PAUSED, g.Query(job.RequestId)!.State);
        g.Submit(Request(g, "RESTART"));
        Assert.Equal(RequestState.RUNNING, g.Query(job.RequestId)!.State);
    }
    [Fact] public void DisconnectRotatesSessionAndNeverAutoResumes()
    {
        using var g = new DemoGateway(); var job = Request(g, "BUILD_A"); g.Submit(job);
        var stale = Request(g, "RESET"); g.Disconnect();
        Assert.Null(g.Query(job.RequestId)); Assert.Equal(ConnectionState.COMM_LOST, g.GetSnapshot().BridgeConnection);
        g.Reconnect(); Assert.Equal("SESSION_EXPIRED", g.Submit(stale).Code);
        for (var i = 0; i < 5; i++) g.Advance();
        Assert.Equal(RequestState.PAUSED, g.Query(job.RequestId)!.State); Assert.Equal(1, g.Executions);
    }
    [Theory][InlineData("RESET", RequestState.CANCELLED)][InlineData("CLEAR_ERROR", RequestState.CLEARED)]
    public void RecoveryDoesNotReportOriginalJobSuccess(string code, RequestState expected)
    {
        using var g = new DemoGateway(); var job = Request(g, "BUILD_A"); g.Submit(job);
        g.Submit(Request(g, "EMER_STOP")); var recovery = g.Submit(Request(g, code));
        Assert.Equal(RequestState.SUCCEEDED, recovery.Request!.State);
        Assert.Equal(expected, g.Query(job.RequestId)!.State);
        Assert.True(g.Submit(Request(g, "LOAD_A")).Accepted);
    }
    [Fact] public void ClearErrorCannotClearRunningJob()
    {
        using var g = new DemoGateway(); var job = Request(g, "BUILD_A"); g.Submit(job);
        Assert.False(g.Submit(Request(g, "CLEAR_ERROR")).Accepted);
        Assert.Equal(RequestState.RUNNING, g.Query(job.RequestId)!.State);
    }
    [Fact] public void FaultAndLostReplyAreDistinctAndQueryDoesNotExecute()
    {
        using var g = new DemoGateway(); g.FailNextJob(); g.LoseNextReply(); var job = Request(g, "BUILD_A");
        Assert.Equal("RESPONSE_UNKNOWN", g.Submit(job).Code);
        Assert.Equal(RequestState.RUNNING, g.Query(job.RequestId)!.State);
        for (var i = 0; i < 4; i++) g.Advance();
        Assert.Equal(RequestState.FAILED, g.Query(job.RequestId)!.State); Assert.Equal(1, g.Executions);
    }
    [Fact] public void ArmingNextFailureDoesNotChangeAnAlreadyRunningJob()
    {
        using var g = new DemoGateway(); var first = Request(g, "BUILD_A"); g.Submit(first); g.FailNextJob();
        for (var i = 0; i < 4; i++) g.Advance();
        Assert.Equal(RequestState.SUCCEEDED, g.Query(first.RequestId)!.State);
        var next = Request(g, "LOAD_A"); g.Submit(next);
        for (var i = 0; i < 4; i++) g.Advance();
        Assert.Equal(RequestState.FAILED, g.Query(next.RequestId)!.State);
    }
    [Fact] public void InvalidVersionIdAndCommandAreRejectedBeforeExecution()
    {
        using var g = new DemoGateway(); var request = Request(g, "BUILD_A");
        Assert.Equal("VERSION_MISMATCH", g.Submit(request with { ProtocolVersion = 2 }).Code);
        Assert.Equal("INVALID_REQUEST", g.Submit(request with { RequestId = "broken" }).Code);
        Assert.Equal("INVALID_COMMAND", g.Submit(request with { Command = "HELP" }).Code);
        Assert.Equal("INVALID_COMMAND", g.Submit(request with { Command = "BUILD_C" }).Code);
        Assert.Equal(0, g.Executions);
    }
    [Fact] public void EventsHaveOrderedSequencesAndImmutableSnapshots()
    {
        using var g = new DemoGateway(); var events = new List<GatewayEvent>(); g.Changed += events.Add;
        var job = Request(g, "BUILD_A"); g.Submit(job); g.Advance(); g.Disconnect(); g.Reconnect();
        Assert.Equal(new long[] { 1, 2, 3, 4 }, events.Select(e => e.Sequence));
        Assert.Equal(RequestState.RUNNING, events[0].Snapshot.Arms[0].Job!.State);
        Assert.Equal(RequestState.PAUSED, events[^1].Snapshot.Arms[0].Job!.State);
        Assert.NotEqual(events[0].SessionGeneration, events[^1].SessionGeneration);
    }
    [Fact] public async Task ViewModelKeepsLatestTwoAndPreservesInputDuringEvents()
    {
        using var g = new DemoGateway(); using var vm = new MainViewModel(g, g);
        await vm.ExecuteAsync("BUILD_A"); await vm.ExecuteAsync("STATUS"); await vm.ExecuteAsync("RETRY"); vm.Input = "BUILD_";
        g.Advance(); g.Disconnect(); g.Reconnect();
        Assert.Equal(new[] { "RETRY", "STATUS" }, vm.History.Select(h => h.Command));
        Assert.Equal("BUILD_", vm.Input); Assert.False(vm.HistoryEmpty);
        Assert.Equal("보류", vm.History[0].State); Assert.Equal(1, g.Executions);
    }
    [Fact] public async Task LostReplyRemainsUnknownUntilExplicitQuery()
    {
        using var g = new DemoGateway(); using var vm = new MainViewModel(g, g);
        g.LoseNextReply(); await vm.ExecuteAsync("BUILD_A"); var id = vm.History[0].RequestId;
        g.Advance(); Assert.Equal("결과 미확인", vm.History[0].State);
        await vm.ExecuteAsync("RETRY"); Assert.Equal(id, vm.History[0].RequestId);
        Assert.Equal("진행 중", vm.History[0].State); Assert.Equal(1, g.Executions);
    }
    [Fact] public async Task InvalidInputShowsErrorAndHelpDoesNotExecute()
    {
        using var g = new DemoGateway(); using var vm = new MainViewModel(g, g);
        await vm.ExecuteAsync("NO_SUCH_COMMAND"); Assert.False(vm.HistoryEmpty); Assert.Equal("입력 오류", vm.History[0].State);
        await vm.ExecuteAsync("HELP"); Assert.True(vm.HelpVisible); Assert.Equal(0, g.Executions);
    }
    [Fact] public async Task ClosingOnlyClosesSessionAndDoesNotReset()
    {
        using var g = new DemoGateway(); using var vm = new MainViewModel(g, g);
        await vm.ExecuteAsync("BUILD_A"); var closed = false; vm.CloseRequested += () => closed = true;
        await vm.ExecuteAsync("QUIT"); Assert.True(closed); Assert.Equal(1, g.Executions);
        Assert.All(g.GetSnapshot().Arms, a => Assert.Null(a.Control));
        Assert.Equal("SESSION_EXPIRED", g.Submit(Request(g, "RESET")).Code);
    }
    [Fact] public void WireFormatRoundTripsWithoutLosingStateAndExportsContractExamples()
    {
        using var g = new DemoGateway(); GatewayEvent? last = null; g.Changed += value => last = value;
        var command = Request(g, "BUILD_LOAD_A"); var reply = g.Submit(command); g.Advance(); g.Advance();
        var snapshot = g.GetSnapshot(); var text = JsonSerializer.Serialize(snapshot, Protocol.Json);
        Assert.Contains("\"protocol_version\": 1", text); Assert.Contains("\"BUILD_DONE\"", text);
        var decoded = JsonSerializer.Deserialize<Snapshot>(text, Protocol.Json)!;
        Assert.Equal(snapshot.Arms[0].Job, decoded.Arms[0].Job);
        Assert.Throws<JsonException>(() => JsonSerializer.Deserialize<CommandRequest>("{\"unexpected\":1}", Protocol.Json));
        var directory = Environment.GetEnvironmentVariable("SO101_CONTRACT_OUTPUT");
        if (directory is not null)
        {
            Directory.CreateDirectory(directory);
            void Save<T>(string name, T value) => File.WriteAllText(Path.Combine(directory, name + ".json"), JsonSerializer.Serialize(value, Protocol.Json));
            Save("session", g.Session); Save("command", command); Save("reply", reply); Save("snapshot", snapshot); Save("event", last);
            Save("challenge", new SupervisionChallenge(1, g.Session.SessionGeneration, g.Session.BridgeInstanceId, "arm2", "demo-epoch", "demo-nonce", 1000));
            Save("ack", new SupervisionAck(1, g.Session.SessionGeneration, g.Session.BridgeInstanceId, "arm2", "demo-epoch", "demo-nonce"));
        }
    }
}
