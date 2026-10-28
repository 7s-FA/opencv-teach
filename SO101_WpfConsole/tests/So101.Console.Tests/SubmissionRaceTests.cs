using System.Net;
using System.Net.Http.Json;
using System.Reflection;
using So101.Console.Core;
using Xunit;

namespace So101.Console.Tests;

public sealed class SubmissionRaceTests
{
    [Theory]
    [InlineData("none")]
    [InlineData("stop")]
    [InlineData("reset")]
    [InlineData("clear")]
    [InlineData("close")]
    [InlineData("disconnect")]
    [InlineData("reconnect")]
    public async Task PendingDiskWriteCannotRenewAnIntentAfterStopOrConnectionLoss(string interruption)
    {
        var folder = Path.Combine(Path.GetTempPath(), "so101-submit-race-" + Protocol.NewId());
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var sent = new List<string>();
        var session = new SessionInfo(1, Protocol.NewId(), Protocol.NewId(), false);
        using var gateway = new LiveGateway(new("https://127.0.0.1:1/", new string('a', 64), new string('0', 64)),
            action => { action(); return Task.CompletedTask; }, folder, record =>
            {
                if (record.Command == "BUILD_A") { entered.TrySetResult(); return release.Task; }
                return Task.CompletedTask;
            });
        var handler = new Handler(async request =>
        {
            var body = (await request.Content!.ReadFromJsonAsync<CommandRequest>(Protocol.Json))!;
            sent.Add(body.Command);
            var record = new RequestRecord(body.RequestId, body.Command, "arm2",
                CommandCatalog.Find(body.Command)!.Kind,
                RequestState.SUCCEEDED, "test only", DateTimeOffset.UtcNow, "IDLE");
            return new(HttpStatusCode.OK) { Content = JsonContent.Create(new CommandReply(1, true, "ACCEPTED", "test", record), options: Protocol.Json) };
        });
        var http = new HttpClient(handler) { BaseAddress = new("https://127.0.0.1:1/") };
        typeof(LiveGateway).GetField("http", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(gateway, http);
        void Online()
        {
            typeof(LiveGateway).GetProperty(nameof(LiveGateway.Session))!.SetValue(gateway, session);
            var snapshot = new Snapshot(1, session.BridgeInstanceId, session.SessionGeneration, 1, false,
                ConnectionState.ONLINE, DateTimeOffset.UtcNow,
                [new("arm2", ConnectionState.ONLINE, null, null), new("arm3", ConnectionState.ONLINE, null, null)]);
            typeof(LiveGateway).GetField("snapshot", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(gateway, snapshot);
        }
        Online();
        try
        {
            var command = new CommandRequest(1, session.SessionGeneration, Protocol.NewId(), "BUILD_A");
            var pending = gateway.SubmitAsync(command);
            await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
            if (interruption is "stop" or "reset" or "clear")
            {
                var cancel = interruption == "stop" ? "EMER_STOP" : interruption == "reset" ? "RESET" : "CLEAR_ERROR";
                var stopped = await gateway.SubmitAsync(new(1, session.SessionGeneration, Protocol.NewId(), cancel));
                Assert.True(stopped.Accepted);
            }
            else if (interruption == "close") gateway.Close();
            else if (interruption != "none")
            {
                typeof(LiveGateway).GetMethod("MarkOffline", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(gateway, ["test outage"]);
                if (interruption == "reconnect") { session = session with { SessionGeneration = Protocol.NewId() }; Online(); }
            }
            release.TrySetResult();
            var result = await pending.WaitAsync(TimeSpan.FromSeconds(2));
            if (interruption == "none") { Assert.Contains("BUILD_A", sent); Assert.True(result.Accepted); return; }
            Assert.DoesNotContain("BUILD_A", sent);
            Assert.False(result.Accepted);
            Assert.Equal(command.RequestId, gateway.Latest(false)?.RequestId);
            Assert.Equal(RequestState.UNKNOWN, gateway.Latest(false)?.State);
        }
        finally { release.TrySetResult(); gateway.Close(); if (Directory.Exists(folder)) Directory.Delete(folder, true); }
    }
    private sealed class Handler(Func<HttpRequestMessage, Task<HttpResponseMessage>> send) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token) => send(request);
    }
}
