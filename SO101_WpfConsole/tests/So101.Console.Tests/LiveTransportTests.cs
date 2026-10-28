using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Security.Authentication;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using So101.Console.Core;
using Xunit;

namespace So101.Console.Tests;

// A loopback TLS/WSS peer only. It cannot reach ROS, Pi, cameras or motors.
public sealed class LiveTransportTests
{
    [Theory]
    [InlineData("snapshot")]
    [InlineData("stale_lease")]
    [InlineData("stale_challenge")]
    [InlineData("frozen_ui")]
    [InlineData("null_snapshot")]
    [InlineData("null_arm")]
    [InlineData("invalid_active_arm")]
    public async Task SessionFramesRetainIdentityOrderingAndUiLiveness(string failure)
    {
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        using var rsa = RSA.Create(2048);
        var request = new CertificateRequest("CN=SO101 loopback", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
        var names = new SubjectAlternativeNameBuilder(); names.AddIpAddress(IPAddress.Loopback);
        request.CertificateExtensions.Add(names.Build());
        using var generated = request.CreateSelfSigned(DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddDays(1));
        using var cert = X509CertificateLoader.LoadPkcs12(generated.Export(X509ContentType.Pfx), null, X509KeyStorageFlags.DefaultKeySet);
        using var listener = new TcpListener(IPAddress.Loopback, 0); listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var session = new SessionInfo(1, Protocol.NewId(), Protocol.NewId(), false);
        var online = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var offline = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var completed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var socketReady = new TaskCompletionSource<WebSocket>(TaskCreationOptions.RunContinuationsAsynchronously);
        var serverFinished = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var requests = new List<string>(); var handlers = new List<Task>();
        var freeze = 0; var seen = new List<long>();
        var server = Task.Run(async () =>
        {
            try
            {
                while (!timeout.IsCancellationRequested)
                {
                    var client = await listener.AcceptTcpClientAsync(timeout.Token);
                    handlers.Add(Serve(client));
                }
            }
            catch (OperationCanceledException) { }
            async Task Serve(TcpClient client)
            {
                using (client)
                using (var tls = new SslStream(client.GetStream()))
                {
                    try
                    {
                        await tls.AuthenticateAsServerAsync(new SslServerAuthenticationOptions { ServerCertificate = cert }, timeout.Token);
                        var header = new StringBuilder(); var one = new byte[1];
                        while (!header.ToString().EndsWith("\r\n\r\n", StringComparison.Ordinal))
                        {
                            if (await tls.ReadAsync(one, timeout.Token) == 0) return;
                            header.Append((char)one[0]);
                            if (header.Length > 16384) throw new IOException("test header too large");
                        }
                        var lines = header.ToString().Split("\r\n"); var path = lines[0].Split(' ')[1];
                        lock (requests) requests.Add(lines[0]);
                        if (path == "/v1/events")
                        {
                            var key = lines.First(x => x.StartsWith("Sec-WebSocket-Key:", StringComparison.OrdinalIgnoreCase)).Split(':', 2)[1].Trim();
                            var accept = Convert.ToBase64String(SHA1.HashData(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
                            await tls.WriteAsync(Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n"), timeout.Token);
                            using var ws = WebSocket.CreateFromStream(tls, true, null, Timeout.InfiniteTimeSpan);
                            socketReady.TrySetResult(ws);
                            await serverFinished.Task.WaitAsync(timeout.Token);
                        }
                        else
                        {
                            var body = JsonSerializer.SerializeToUtf8Bytes(session, Protocol.Json);
                            var response = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " + body.Length + "\r\n\r\n");
                            await tls.WriteAsync(response, timeout.Token); await tls.WriteAsync(body, timeout.Token);
                        }
                    }
                    catch (Exception exc) when (exc is IOException or OperationCanceledException or AuthenticationException) { }
                }
            }
        });
        var directory = Path.Combine(Path.GetTempPath(), "so101-wss-" + Protocol.NewId());
        using var gateway = new LiveGateway(new($"https://127.0.0.1:{port}/", new string('a', 64), Convert.ToHexString(SHA256.HashData(cert.RawData))),
            async action => { if (Volatile.Read(ref freeze) != 0) await Task.Delay(1000); action(); }, directory);
        gateway.Changed += value =>
        {
            lock (seen) seen.Add(value.Sequence);
            if (value.Snapshot.BridgeConnection == ConnectionState.COMM_LOST) offline.TrySetResult();
            else if (value.Snapshot.Arms[0].Job?.State == RequestState.SUCCEEDED) completed.TrySetResult();
            else online.TrySetResult();
        };
        try
        {
            await gateway.StartAsync(); var socket = await socketReady.Task.WaitAsync(timeout.Token);
            async Task Send(object value) => await socket.SendAsync(JsonSerializer.SerializeToUtf8Bytes(value, Protocol.Json), WebSocketMessageType.Text, true, timeout.Token);
            async Task<JsonDocument> Receive()
            {
                var data = new byte[8192]; var result = await socket.ReceiveAsync(data, timeout.Token);
                Assert.Equal(WebSocketMessageType.Text, result.MessageType);
                return JsonDocument.Parse(data.AsMemory(0, result.Count));
            }
            object Lease(string generation) => new { type = "lease", protocol_version = 1, bridge_instance_id = session.BridgeInstanceId, session_generation = generation, nonce = "lease-nonce" };
            object Challenge(string generation) => new { type = "supervision_challenge", payload = new SupervisionChallenge(1, generation, session.BridgeInstanceId, "arm2", Protocol.NewId(), Protocol.NewId(), 800) };
            var id = Protocol.NewId();
            GatewayEvent Frame(long sequence, RequestState state)
            {
                var snapshot = new Snapshot(1, session.BridgeInstanceId, session.SessionGeneration, sequence, false, ConnectionState.ONLINE, DateTimeOffset.UtcNow,
                    [new("arm2", ConnectionState.ONLINE, new(id, "BUILD_LOAD_B", "arm2", CommandKind.JOB, state, "loopback only", DateTimeOffset.UtcNow), null), new("arm3", ConnectionState.ONLINE, null, null)]);
                return new(1, session.BridgeInstanceId, session.SessionGeneration, sequence, "state_changed", DateTimeOffset.UtcNow, snapshot);
            }
            await Send(Lease(session.SessionGeneration));
            using (var ack = await Receive()) Assert.Equal("lease_ack", ack.RootElement.GetProperty("type").GetString());
            await Send(Challenge(session.SessionGeneration));
            using (var ack = await Receive())
            {
                Assert.Equal("supervision_ack", ack.RootElement.GetProperty("type").GetString());
                Assert.Equal(session.SessionGeneration, ack.RootElement.GetProperty("payload").GetProperty("session_generation").GetString());
            }
            await Send(Frame(2, RequestState.BUILD_DONE)); await online.Task.WaitAsync(timeout.Token);
            Assert.False(gateway.GetSnapshot().Arms[0].Job!.Terminal);
            await Send(Frame(1, RequestState.FAILED));
            // A subsequent acknowledged lease proves the older frame has been consumed.
            await Send(Lease(session.SessionGeneration)); using (var ack = await Receive()) Assert.Equal("lease_ack", ack.RootElement.GetProperty("type").GetString());
            Assert.Equal(RequestState.BUILD_DONE, gateway.GetSnapshot().Arms[0].Job!.State);
            if (failure == "snapshot")
            {
                await Send(Frame(3, RequestState.SUCCEEDED)); await completed.Task.WaitAsync(timeout.Token);
                await Send(Frame(4, RequestState.RUNNING) with { SessionGeneration = Protocol.NewId() });
            }
            else if (failure == "null_snapshot") await Send(Frame(3, RequestState.RUNNING) with { Snapshot = null! });
            else if (failure == "null_arm")
            {
                var bad = Frame(3, RequestState.RUNNING);
                await Send(bad with { Snapshot = bad.Snapshot with { Arms = [null!, new("arm3", ConnectionState.ONLINE, null, null)] } });
            }
            else if (failure == "invalid_active_arm")
            {
                var bad = Frame(3, RequestState.RUNNING);
                await Send(bad with { Snapshot = bad.Snapshot with { ActiveArm = "missing-arm" } });
            }
            else if (failure == "stale_lease") await Send(Lease(Protocol.NewId()));
            else if (failure == "stale_challenge") await Send(Challenge(Protocol.NewId()));
            else
            {
                Volatile.Write(ref freeze, 1); await Send(Challenge(session.SessionGeneration));
            }
            await offline.Task.WaitAsync(timeout.Token);
            Assert.Equal(ConnectionState.COMM_LOST, gateway.GetSnapshot().BridgeConnection);
            Assert.Equal(failure == "snapshot" ? RequestState.SUCCEEDED : RequestState.BUILD_DONE, gateway.GetSnapshot().Arms[0].Job!.State);
            lock (requests) Assert.DoesNotContain(requests, x => x.Contains("/v1/commands"));
            lock (seen) Assert.DoesNotContain(1L, seen);
        }
        finally
        {
            gateway.Close(); serverFinished.TrySetResult(); timeout.Cancel();
            await server; await Task.WhenAll(handlers);
            if (Directory.Exists(directory)) Directory.Delete(directory, true);
        }
    }
}
