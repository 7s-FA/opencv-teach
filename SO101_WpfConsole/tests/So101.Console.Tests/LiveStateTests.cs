using System.Text.Json;
using So101.Console.Core;
using Xunit;
namespace So101.Console.Tests;

public sealed class LiveStateTests : IDisposable
{
    private readonly string path = Path.Combine(Path.GetTempPath(), "so101-live-test-" + Guid.NewGuid().ToString("N"));
    private static BridgeProfile Profile(string url = "https://127.0.0.1:8443/") => new(url, new string('a', 64), new string('0', 64));
    private LiveGateway Create(BridgeProfile? profile = null) => new(profile ?? Profile(), action => { action(); return Task.CompletedTask; }, path);
    [Fact]
    public void PlaintextTransportIsRejectedBeforeNetworkAccess()
    { Assert.Throws<ArgumentException>(() => Create(Profile("http://127.0.0.1:8443/"))); }
    [Fact]
    public void TokenCannotInjectHeaders()
    { Assert.Throws<ArgumentException>(() => Create(Profile() with { Token = new string('a', 32) + "\r\nX-Test: bad" })); }
    [Fact]
    public void CorruptPendingIdentityFailsClosed()
    {
        Directory.CreateDirectory(path);
        var record = new RequestRecord(new string('b', 32), "BUILD_A", "arm2", CommandKind.JOB, RequestState.UNKNOWN, "pending", DateTimeOffset.UtcNow);
        File.WriteAllText(Path.Combine(path, new string('c', 32) + ".json"), JsonSerializer.Serialize(record, Protocol.Json));
        Assert.Throws<InvalidDataException>(() => Create());
    }
    [Fact]
    public async Task RestartPreservesUncertainIdAndNeverSendsWhileDisconnected()
    {
        Directory.CreateDirectory(path);
        var key = Protocol.NewId();
        var record = new RequestRecord(key, "BUILD_A", "arm2", CommandKind.JOB, RequestState.ACCEPTED, "pending", DateTimeOffset.UtcNow);
        File.WriteAllText(Path.Combine(path, key + ".json"), JsonSerializer.Serialize(record, Protocol.Json));
        using var gateway = Create();
        Assert.Equal(key, gateway.Latest(false)?.RequestId);
        Assert.Equal(RequestState.UNKNOWN, gateway.Latest(false)?.State);
        var reply = await gateway.SubmitAsync(new(1, gateway.Session.SessionGeneration, Protocol.NewId(), "BUILD_B"));
        Assert.False(reply.Accepted);
        Assert.Single(Directory.GetFiles(path, "*.json"));
    }
    [Fact]
    public async Task KnownFinalResultSurvivesConnectionLoss()
    {
        var demo = new DemoGateway(); using var vm = new MainViewModel(demo, demo);
        await vm.ExecuteAsync("BUILD_A");
        for (var i = 0; i < 4; i++) demo.Advance();
        Assert.Equal("완료", vm.History[0].State);
        demo.Disconnect();
        Assert.Equal("완료", vm.History[0].State);
        Assert.Contains("완료", vm.Arm2.Job);
    }
    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(2)]
    public async Task TlsEnforcesPinnedIdentityAndAcceptsOnlyTheMatchingServer(int mode)
    {
        using var rsa = System.Security.Cryptography.RSA.Create(2048);
        var request = new System.Security.Cryptography.X509Certificates.CertificateRequest("CN=SO101 test",
            rsa, System.Security.Cryptography.HashAlgorithmName.SHA256, System.Security.Cryptography.RSASignaturePadding.Pkcs1);
        var names = new System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder();
        names.AddIpAddress(System.Net.IPAddress.Parse(mode == 1 ? "127.0.0.2" : "127.0.0.1"));
        request.CertificateExtensions.Add(names.Build());
        using var generated = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddDays(1));
        // Schannel needs a named key; reimport only the temporary test-server key.
        // https://github.com/dotnet/runtime/issues/114640
        using var certificate = System.Security.Cryptography.X509Certificates.X509CertificateLoader.LoadPkcs12(
            generated.Export(System.Security.Cryptography.X509Certificates.X509ContentType.Pfx), null,
            System.Security.Cryptography.X509Certificates.X509KeyStorageFlags.DefaultKeySet);
        using var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start(); var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
        var receivedHttp = false; string? serverFailure = null;
        var server = Task.Run(async () =>
        {
            using var socket = await listener.AcceptTcpClientAsync(timeout.Token);
            using var tls = new System.Net.Security.SslStream(socket.GetStream());
            try
            {
                await tls.AuthenticateAsServerAsync(new System.Net.Security.SslServerAuthenticationOptions { ServerCertificate = certificate }, timeout.Token);
                receivedHttp = await tls.ReadAsync(new byte[1], timeout.Token) > 0;
            }
            catch (Exception exc) when (exc is IOException or System.Security.Authentication.AuthenticationException or OperationCanceledException) { serverFailure = exc.ToString(); }
        });
        var pin = mode != 0 ? Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(certificate.RawData)) : new string('0', 64);
        using var gateway = Create(Profile($"https://127.0.0.1:{port}/") with { CertificateSha256 = pin });
        var failed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        gateway.Changed += _ => failed.TrySetResult();
        await gateway.StartAsync(); await failed.Task.WaitAsync(timeout.Token); gateway.Close();
        await server.WaitAsync(timeout.Token);
        Assert.True(receivedHttp == (mode == 2), $"TLS positive/negative control mismatch. Server: {serverFailure ?? "no exception"}; client: {gateway.ConnectionDetail}");
        Assert.Equal(ConnectionState.COMM_LOST, gateway.GetSnapshot().BridgeConnection);
    }
    [Fact]
    public async Task CloseAndDisposeAreIdempotentAfterCleanup()
    {
        var gateway = Create(); gateway.Dispose();
        await Task.Delay(30);
        gateway.Close(); gateway.Dispose(); await gateway.ReconnectAsync();
        Assert.Equal(ConnectionState.COMM_LOST, gateway.GetSnapshot().BridgeConnection);
    }
    [Theory]
    [InlineData(RequestState.ACCEPTED, "제어 요청 결과 대기")]
    [InlineData(RequestState.UNKNOWN, "제어 결과 미확인 · 기존 요청 조회 필요")]
    public void IdleJobDoesNotHideAnUnfinishedControl(RequestState state, string expected)
    {
        using var gateway = new SnapshotGateway(state); using var view = new MainViewModel(gateway);
        Assert.Equal(expected, view.SystemState);
        Assert.Equal("online", view.Arm2.ConnectionTone);
    }
    private sealed class SnapshotGateway(RequestState state) : IControlGateway
    {
        public SessionInfo Session { get; } = new(1, Protocol.NewId(), Protocol.NewId(), false);
        public event Action<GatewayEvent>? Changed { add { } remove { } }
        public Snapshot GetSnapshot() => new(1, Session.BridgeInstanceId, Session.SessionGeneration, 1, false,
            ConnectionState.ONLINE, DateTimeOffset.UtcNow,
            [new("arm2", ConnectionState.ONLINE, null, new(Protocol.NewId(), "RESET", "arm2", CommandKind.CONTROL, state, "result pending", DateTimeOffset.UtcNow)),
             new("arm3", ConnectionState.ONLINE, null, null)]);
        public string? UnavailableReason(string command) => null;
        public RequestRecord? Latest(bool control) => null;
        public Task<CommandReply> SubmitAsync(CommandRequest request) => throw new NotSupportedException();
        public Task<RequestRecord?> QueryAsync(string requestId) => Task.FromResult<RequestRecord?>(null);
        public void Close() { }
        public void Dispose() { }
    }
    public void Dispose() { if (Directory.Exists(path)) Directory.Delete(path, true); }
}
