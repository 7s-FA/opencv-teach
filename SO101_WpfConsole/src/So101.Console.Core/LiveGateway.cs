using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Net.Security;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;

namespace So101.Console.Core;

public sealed record BridgeProfile(string Url, string Token, string CertificateSha256, string Name = "SO-101 관제");
public interface ILiveControls
{
    string ConnectionDetail { get; }
    Task ReconnectAsync();
}

/// <summary>One WPF UI consumer. Network callbacks reach it through the supplied dispatcher.</summary>
public sealed class LiveGateway : IControlGateway, ILiveControls
{
    private readonly BridgeProfile profile;
    private readonly HttpClient http;
    private readonly Func<Action, Task> dispatch;
    private readonly string pendingDirectory;
    private readonly Func<RequestRecord, Task> persistPending;
    private readonly Dictionary<string, RequestRecord> unresolved = [];
    private readonly Dictionary<string, Task> pendingWrites = [];
    private readonly HashSet<string> activeSubmissions = [];
    private readonly CancellationTokenSource lifetime = new();
    private CancellationTokenSource? connection;
    private Task? loop;
    private bool closed, disposed;
    private long intentRevision;
    private Snapshot snapshot = new(1, new string('0', 32), new string('0', 32), 0, false,
        ConnectionState.COMM_LOST, DateTimeOffset.MinValue,
        [new("arm2", ConnectionState.COMM_LOST, null, null), new("arm3", ConnectionState.COMM_LOST, null, null)]);
    public SessionInfo Session { get; private set; } = new(1, new string('0', 32), new string('0', 32), false);
    public string ConnectionDetail { get; private set; } = "중계 연결 준비 중";
    public event Action<GatewayEvent>? Changed;

    public LiveGateway(BridgeProfile profile, Func<Action, Task> dispatch, string pendingDirectory, Func<RequestRecord, Task>? persistPending = null)
    {
        if (!Uri.TryCreate(profile.Url, UriKind.Absolute, out var uri) || uri.Scheme != "https" ||
            !string.IsNullOrEmpty(uri.UserInfo) || uri.AbsolutePath != "/" || !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
            throw new ArgumentException("중계 주소는 경로 없는 HTTPS 주소여야 합니다.");
        if (profile.Token is not { Length: >= 32 } || !profile.Token.All(c => char.IsAsciiLetterOrDigit(c) || c is '-' or '_' or '.' or '~') || profile.CertificateSha256 is not { Length: 64 } ||
            !profile.CertificateSha256.All(Uri.IsHexDigit)) throw new ArgumentException("연결 토큰 또는 인증서 지문 형식 오류");
        this.profile = profile; this.dispatch = dispatch; this.pendingDirectory = pendingDirectory;
        this.persistPending = persistPending ?? (record => Task.Run(() => WritePendingFile(record)));
        Directory.CreateDirectory(pendingDirectory);
        foreach (var file in Directory.EnumerateFiles(pendingDirectory, "*.json"))
        {
            var record = JsonSerializer.Deserialize<RequestRecord>(File.ReadAllText(file), Protocol.Json)
                ?? throw new InvalidDataException("미확정 요청 기록 오류");
            if (!Protocol.ValidId(record.RequestId) || Path.GetFileNameWithoutExtension(file) != record.RequestId ||
                string.IsNullOrWhiteSpace(record.Command) || CommandCatalog.Find(record.Command) is not { } command || command.Kind != record.Kind || record.Arm is not ("arm2" or "arm3")) throw new InvalidDataException("미확정 요청 기록 형식 오류");
            unresolved[record.RequestId] = record with { State = RequestState.UNKNOWN };
        }
        var handler = new HttpClientHandler { AllowAutoRedirect = false,
            ServerCertificateCustomValidationCallback = (_, cert, _, errors) => ValidateCertificate(cert, errors) };
        http = new(handler) { BaseAddress = uri, Timeout = TimeSpan.FromSeconds(3) };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", profile.Token);
    }

    private bool ValidateCertificate(X509Certificate? certificate, SslPolicyErrors errors)
    {
        if (certificate is null || (errors & (SslPolicyErrors.RemoteCertificateNotAvailable | SslPolicyErrors.RemoteCertificateNameMismatch)) != 0) return false;
        using var cert = X509CertificateLoader.LoadCertificate(certificate.GetRawCertData());
        return DateTime.UtcNow >= cert.NotBefore.ToUniversalTime() && DateTime.UtcNow <= cert.NotAfter.ToUniversalTime() &&
            CryptographicOperations.FixedTimeEquals(SHA256.HashData(cert.RawData), Convert.FromHexString(profile.CertificateSha256));
    }
    public Task StartAsync() { if (!closed) loop ??= RunAsync(); return Task.CompletedTask; }
    public Task ReconnectAsync() { CancelConnection(); return StartAsync(); }
    private void CancelConnection()
    {
        intentRevision++;
        try { connection?.Cancel(); } catch (ObjectDisposedException) { }
    }

    private async Task RunAsync()
    {
        while (!lifetime.IsCancellationRequested)
        {
            using var attempt = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
            connection = attempt;
            string? generation = null;
            try
            {
                using var response = await http.PostAsJsonAsync("v1/session", new { protocol_version = 1 }, attempt.Token).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                var session = await response.Content.ReadFromJsonAsync<SessionInfo>(Protocol.Json, attempt.Token).ConfigureAwait(false)
                    ?? throw new InvalidDataException("세션 응답 없음");
                if (session.ProtocolVersion != 1 || !Protocol.ValidId(session.SessionGeneration) || !Protocol.ValidId(session.BridgeInstanceId))
                    throw new InvalidDataException("세션 규약 불일치");
                generation = session.SessionGeneration;
                await dispatch(() => { Session = session; ConnectionDetail = "양방향 연결 확인 중"; }).WaitAsync(attempt.Token).ConfigureAwait(false);
                using var socket = new ClientWebSocket();
                socket.Options.SetRequestHeader("Authorization", "Bearer " + profile.Token);
                socket.Options.SetRequestHeader("X-Session-Generation", generation);
                socket.Options.RemoteCertificateValidationCallback = (_, cert, _, errors) => ValidateCertificate(cert, errors);
                var address = new UriBuilder(http.BaseAddress!) { Scheme = "wss", Path = "v1/events" }.Uri;
                await socket.ConnectAsync(address, attempt.Token).ConfigureAwait(false);
                Task recovery = Task.CompletedTask;
                long nextRecovery = 0;
                while (socket.State == WebSocketState.Open && !attempt.IsCancellationRequested)
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(attempt.Token);
                    timeout.CancelAfter(TimeSpan.FromSeconds(2));
                    var text = await ReceiveAsync(socket, timeout.Token).ConfigureAwait(false);
                    var received = System.Diagnostics.Stopwatch.GetTimestamp();
                    using var document = JsonDocument.Parse(text);
                    var root = document.RootElement;
                    if (await HandleFrameAsync(root, socket, session, generation, received, attempt.Token).ConfigureAwait(false) is { } value)
                    {
                        if (recovery.IsCompleted && System.Diagnostics.Stopwatch.GetTimestamp() >= nextRecovery &&
                            value.Snapshot.Arms.Any(a => a.Connection == ConnectionState.ONLINE))
                        {
                            nextRecovery = System.Diagnostics.Stopwatch.GetTimestamp() + 2 * System.Diagnostics.Stopwatch.Frequency;
                            // Bounded read-only retries also cover staggered arm reconnects
                            // and transient storage failures within the same WS session.
                            await dispatch(() => recovery = RecoverAsync(generation, attempt.Token)).WaitAsync(attempt.Token).ConfigureAwait(false);
                        }
                    }
                }
            }
            catch (Exception exc) when (exc is HttpRequestException or WebSocketException or IOException or InvalidDataException or JsonException or OperationCanceledException or TimeoutException or InvalidOperationException or KeyNotFoundException)
            {
                if (!closed) await dispatch(() => MarkOffline(exc is HttpRequestException h && h.StatusCode == HttpStatusCode.Conflict
                    ? "다른 관제 화면이 연결되어 있습니다." : "연결 확인 필요 · " + SafeError(exc))).ConfigureAwait(false);
            }
            finally
            {
                if (generation is not null)
                {
                    try { using var request = Request(HttpMethod.Delete, "v1/session", generation); using var result = await http.SendAsync(request).ConfigureAwait(false); }
                    catch (Exception exc) when (exc is HttpRequestException or OperationCanceledException or ObjectDisposedException) { }
                }
                if (ReferenceEquals(connection, attempt)) connection = null;
            }
            try { await Task.Delay(1500, lifetime.Token).ConfigureAwait(false); } catch (OperationCanceledException) { break; }
        }
    }
    private async Task<GatewayEvent?> HandleFrameAsync(JsonElement root, ClientWebSocket socket,
        SessionInfo session, string generation, long received, CancellationToken token)
    {
        var type = root.TryGetProperty("type", out var field) ? field.GetString() : null;
        switch (type)
        {
            case "lease":
                await AcknowledgeLeaseAsync(root, socket, session, generation, received, token).ConfigureAwait(false);
                return null;
            case "supervision_challenge":
                await AcknowledgeSupervisionAsync(root, socket, session, generation, received, token).ConfigureAwait(false);
                return null;
            default:
                return await ApplyFrameAsync(root, session, generation, token).ConfigureAwait(false);
        }
    }

    private async Task AcknowledgeLeaseAsync(JsonElement root, ClientWebSocket socket,
        SessionInfo session, string generation, long received, CancellationToken token)
    {
        if (root.GetProperty("session_generation").GetString() != generation || root.GetProperty("protocol_version").GetInt32() != 1 ||
            root.GetProperty("bridge_instance_id").GetString() != session.BridgeInstanceId) throw new InvalidDataException("세션 변경");
        var nonce = root.GetProperty("nonce").GetString();
        await dispatch(() => { }).WaitAsync(TimeSpan.FromMilliseconds(800), token).ConfigureAwait(false);
        if (System.Diagnostics.Stopwatch.GetElapsedTime(received).TotalMilliseconds >= 800) throw new TimeoutException("화면 응답 지연");
        await SendAsync(socket, new { type = "lease_ack", protocol_version = 1, bridge_instance_id = session.BridgeInstanceId, session_generation = generation, nonce }, token).ConfigureAwait(false);
    }

    private async Task AcknowledgeSupervisionAsync(JsonElement root, ClientWebSocket socket,
        SessionInfo session, string generation, long received, CancellationToken token)
    {
        var challenge = root.GetProperty("payload").Deserialize<SupervisionChallenge>(Protocol.Json) ?? throw new InvalidDataException("생존 확인 오류");
        if (challenge.SessionGeneration != generation || challenge.BridgeInstanceId != session.BridgeInstanceId || challenge.ProtocolVersion != 1)
            throw new InvalidDataException("생존 확인 세대 불일치");
        await dispatch(() => { }).WaitAsync(TimeSpan.FromMilliseconds(800), token).ConfigureAwait(false);
        if (System.Diagnostics.Stopwatch.GetElapsedTime(received).TotalMilliseconds >= Math.Min(challenge.ExpiresInMs, 800)) return;
        await SendAsync(socket, new { type = "supervision_ack", payload = new SupervisionAck(1, generation, session.BridgeInstanceId, challenge.Arm, challenge.Epoch, challenge.Nonce) }, token).ConfigureAwait(false);
    }

    private async Task<GatewayEvent> ApplyFrameAsync(JsonElement root, SessionInfo session,
        string generation, CancellationToken token)
    {
        var value = root.Deserialize<GatewayEvent>(Protocol.Json) ?? throw new InvalidDataException("상태 응답 오류");
        if (value.ProtocolVersion != 1 || value.SessionGeneration != generation || value.BridgeInstanceId != session.BridgeInstanceId ||
            value.Snapshot is null || value.Snapshot.ProtocolVersion != Protocol.Version ||
            value.Snapshot.ActiveArm is not ("arm2" or "arm3") ||
            value.Snapshot.SessionGeneration != generation || value.Snapshot.BridgeInstanceId != session.BridgeInstanceId ||
            value.Snapshot.Sequence != value.Sequence || value.Snapshot.Arms is not { Count: 2 } ||
            value.Snapshot.Arms.Any(a => a is null) ||
            !value.Snapshot.Arms.Select(a => a.Arm).Order().SequenceEqual(new[] { "arm2", "arm3" })) throw new InvalidDataException("상태 규약 불일치");
        // Every event is a full authoritative snapshot. A skipped cursor is
        // already resynchronized by this frame; an older cursor is rejected.
        await dispatch(() => ApplySnapshot(value)).WaitAsync(token).ConfigureAwait(false);
        return value;

    }

    private static string SafeError(Exception exc) => exc switch
    {
        JsonException or InvalidDataException => "수신 규약 오류",
        TimeoutException or OperationCanceledException => "응답 시간 초과",
        HttpRequestException => "주소·인증·인증서 또는 중계 실행 상태를 확인하세요",
        _ => "통신 채널이 종료되었습니다"
    };
    private static async Task<string> ReceiveAsync(ClientWebSocket socket, CancellationToken token)
    {
        var bytes = new byte[65536]; var count = 0;
        while (true)
        {
            var result = await socket.ReceiveAsync(bytes.AsMemory(count), token).ConfigureAwait(false);
            if (result.MessageType != WebSocketMessageType.Text) throw new WebSocketException("이벤트 채널 종료");
            count += result.Count;
            if (result.EndOfMessage) return Encoding.UTF8.GetString(bytes, 0, count);
            if (count == bytes.Length) throw new InvalidDataException("이벤트 크기 제한 초과");
        }
    }
    private static async Task SendAsync<T>(ClientWebSocket socket, T value, CancellationToken token)
        => await socket.SendAsync(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(value, Protocol.Json)).AsMemory(), WebSocketMessageType.Text, true, token).ConfigureAwait(false);
    private HttpRequestMessage Request(HttpMethod method, string path, string? generation = null)
    {
        var message = new HttpRequestMessage(method, path);
        message.Headers.Add("X-Session-Generation", generation ?? Session.SessionGeneration); return message;
    }
    private void ApplySnapshot(GatewayEvent value)
    {
        if (closed || value.SessionGeneration != Session.SessionGeneration) return;
        if (snapshot.SessionGeneration == value.SessionGeneration && value.Sequence <= snapshot.Sequence) return;
        snapshot = value.Snapshot; ConnectionDetail = snapshot.StorageError ?? "중계 연결됨 · 실제 실행부 상태 수신 중";
        foreach (var record in snapshot.Arms.SelectMany(a => new[] { a.Job, a.Control }).OfType<RequestRecord>()) UpdatePending(record);
        Changed?.Invoke(value);
    }
    private void MarkOffline(string detail)
    {
        intentRevision++;
        ConnectionDetail = detail; snapshot = snapshot with { BridgeConnection = ConnectionState.COMM_LOST };
        Changed?.Invoke(new(1, snapshot.BridgeInstanceId, snapshot.SessionGeneration, snapshot.Sequence, "connection_changed", DateTimeOffset.UtcNow, snapshot));
    }
    private void WritePendingFile(RequestRecord record)
    {
        var path = Path.Combine(pendingDirectory, record.RequestId + ".json"); var temp = path + ".tmp";
        using (var stream = new FileStream(temp, FileMode.Create, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
        { JsonSerializer.Serialize(stream, record, Protocol.Json); stream.Flush(true); }
        File.Move(temp, path, true);
    }
    private void UpdatePending(RequestRecord record)
    {
        if (!unresolved.ContainsKey(record.RequestId)) return;
        unresolved[record.RequestId] = record;
        if (record.Terminal) _ = RemovePendingAsync(record.RequestId);
    }
    private async Task RemovePendingAsync(string key)
    {
        try
        {
            if (pendingWrites.TryGetValue(key, out var writing))
                try { await writing; } catch (Exception exc) when (exc is IOException or UnauthorizedAccessException) { }
            await Task.Run(() => File.Delete(Path.Combine(pendingDirectory, key + ".json")));
            unresolved.Remove(key); pendingWrites.Remove(key);
        }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException)
        { ConnectionDetail = "결과 확인됨 · 로컬 요청 기록 정리 실패"; }
    }
    private Task WritePendingAsync(RequestRecord record)
    {
        var writing = persistPending(record); pendingWrites[record.RequestId] = writing; return writing;
    }
    private async Task SaveStopAsync(RequestRecord pending)
    {
        try { await WritePendingAsync(pending); }
        catch (Exception exc) when (exc is IOException or UnauthorizedAccessException)
        { ConnectionDetail = "정지 전송 중 · 로컬 기록 저장 실패"; }
    }
    private void LocalChanged() => Changed?.Invoke(new(1, snapshot.BridgeInstanceId, snapshot.SessionGeneration,
        snapshot.Sequence, "state_changed", DateTimeOffset.UtcNow, snapshot));
    private async Task RecoverAsync(string generation, CancellationToken token)
    {
        foreach (var record in unresolved.Values.Where(r => !r.Terminal).ToArray())
        {
            if (token.IsCancellationRequested || generation != Session.SessionGeneration) break;
            if (!activeSubmissions.Contains(record.RequestId) && snapshot.Arms.Any(a => a.Arm == record.Arm && a.Connection == ConnectionState.ONLINE))
                await QueryAsync(record.RequestId, generation, token);
        }
    }
    public Snapshot GetSnapshot() => snapshot;
    public RequestRecord? Latest(bool control)
    {
        var pending = unresolved.Values.Where(r => !r.Terminal && (r.Kind == CommandKind.CONTROL) == control)
            .OrderByDescending(r => r.UpdatedAt).FirstOrDefault();
        return pending ?? snapshot.Arms.SelectMany(a => new[] { a.Job, a.Control }).OfType<RequestRecord>()
            .Where(r => (r.Kind == CommandKind.CONTROL) == control).OrderByDescending(r => r.UpdatedAt).FirstOrDefault();
    }
    public string? UnavailableReason(string command)
    {
        var definition = CommandCatalog.Find(command);
        if (definition is null) return "알 수 없는 명령";
        if (definition.Kind == CommandKind.LOCAL) return null;
        if (snapshot.BridgeConnection != ConnectionState.ONLINE) return "중계 연결 확인 필요";
        if (definition.Kind == CommandKind.QUERY) return Latest(command == "RETRY_CONTROL") is null ? "조회할 요청이 없습니다." : null;
        if (command == "EMER_STOP") return null;
        if (snapshot.StorageError is not null) return snapshot.StorageError;
        if (snapshot.Arms.Any(a => a.Control is { Terminal: false }) || unresolved.Values.Any(r => r.Kind == CommandKind.CONTROL && !r.Terminal))
            return "이전 제어 요청을 RETRY_CONTROL로 확인하세요.";
        var target = definition.Arm ?? snapshot.ActiveArm;
        if (snapshot.Arms.First(a => a.Arm == target).Connection != ConnectionState.ONLINE) return "대상 팔의 양방향 연결 확인 필요";
        if (definition.Kind == CommandKind.JOB && (snapshot.Arms.Any(a => a.Job is { Terminal: false }) || unresolved.Values.Any(r => r.Kind == CommandKind.JOB && !r.Terminal)))
            return "진행 중이거나 미확정인 업무를 먼저 조회하세요.";
        return null;
    }
    public async Task<CommandReply> SubmitAsync(CommandRequest request)
    {
        if (request.Command is "EMER_STOP" or "RESET" or "CLEAR_ERROR") intentRevision++;
        if (!activeSubmissions.Add(request.RequestId))
            return new(1, false, "REQUEST_IN_FLIGHT", "같은 요청을 전송 중입니다.", null);
        try { return await SubmitCoreAsync(request); }
        finally { activeSubmissions.Remove(request.RequestId); }
    }
    private async Task<CommandReply> SubmitCoreAsync(CommandRequest request)
    {
        if (closed || request.ProtocolVersion != Protocol.Version || !Protocol.ValidId(request.RequestId) ||
            request.SessionGeneration != Session.SessionGeneration)
            return new(1, false, "INVALID_REQUEST", "종료되었거나 변경된 연결의 요청입니다.", null);
        var revision = intentRevision;
        var reason = UnavailableReason(request.Command);
        if (reason is not null) return new(1, false, "NOT_ALLOWED", reason, null);
        var definition = CommandCatalog.Find(request.Command)!;
        var arm = definition.Arm ?? snapshot.ActiveArm;
        var pending = new RequestRecord(request.RequestId, request.Command, arm, definition.Kind, RequestState.UNKNOWN,
            "전송 결과 미확인 · 같은 ID로 조회", DateTimeOffset.UtcNow);
        unresolved[request.RequestId] = pending; LocalChanged();
        if (request.Command != "EMER_STOP")
        {
            try { await WritePendingAsync(pending); }
            catch (Exception exc) when (exc is IOException or UnauthorizedAccessException)
            {
                unresolved.Remove(request.RequestId); LocalChanged();
                return new(1, false, "STORAGE_ERROR", "로컬 요청 기록 저장 실패 · 전송하지 않았습니다.", null);
            }
        }
        // Disk completion cannot renew an intent across a stop or connection loss.
        // Retain its ID for query-only reconciliation; never replay it as execution.
        if (request.Command != "EMER_STOP" && (closed || revision != intentRevision ||
            request.SessionGeneration != Session.SessionGeneration || snapshot.BridgeConnection != ConnectionState.ONLINE ||
            !snapshot.Arms.Any(a => a.Arm == arm && a.Connection == ConnectionState.ONLINE) || snapshot.StorageError is not null))
        {
            pending = pending with { Detail = "정지·연결 상태 변경 · 전송하지 않고 같은 ID 조회 대기" };
            unresolved[request.RequestId] = pending; LocalChanged();
            return new(1, false, "REQUEST_SUPERSEDED", pending.Detail, pending);
        }
        try
        {
            using var message = Request(HttpMethod.Post, "v1/commands", request.SessionGeneration);
            message.Content = JsonContent.Create(request, options: Protocol.Json);
            // Stop reaches the HTTP handler before any optional local disk write.
            var sending = http.SendAsync(message, lifetime.Token);
            if (request.Command == "EMER_STOP") _ = SaveStopAsync(pending);
            using var response = await sending;
            var body = await response.Content.ReadAsStringAsync(lifetime.Token);
            using var envelope = JsonDocument.Parse(body);
            if (!envelope.RootElement.TryGetProperty("accepted", out _)) throw new HttpRequestException("요청 결과 확인 필요", null, response.StatusCode);
            var result = JsonSerializer.Deserialize<CommandReply>(body, Protocol.Json) ?? throw new InvalidDataException("접수 응답 없음");
            if (result.ProtocolVersion != 1 || (result.Request is { } receipt && (receipt.RequestId != request.RequestId || receipt.Command != request.Command)))
                throw new InvalidDataException("접수 응답 ID 불일치");
            if (result.Request is not null) UpdatePending(result.Request);
            else if (!result.Accepted)
            {
                await RemovePendingAsync(request.RequestId);
            }
            return result;
        }
        catch (Exception exc) when (exc is HttpRequestException or OperationCanceledException or IOException or InvalidDataException or JsonException)
        { return new(1, true, "RESPONSE_UNKNOWN", "접수 응답 미확인 · RETRY로 같은 요청을 조회하세요. 자동 재실행하지 않습니다.", pending); }
    }
    public Task<RequestRecord?> QueryAsync(string requestId)
        => QueryAsync(requestId, Session.SessionGeneration, lifetime.Token);
    private async Task<RequestRecord?> QueryAsync(string requestId, string generation, CancellationToken token)
    {
        if (!Protocol.ValidId(requestId)) return null;
        try
        {
            var path = "v1/requests/" + requestId;
            if (unresolved.TryGetValue(requestId, out var intent))
                path += "?command=" + Uri.EscapeDataString(intent.Command) + "&arm=" + Uri.EscapeDataString(intent.Arm);
            using var message = Request(HttpMethod.Get, path, generation);
            using var response = await http.SendAsync(message, token);
            if (response.StatusCode == HttpStatusCode.NotFound)
                return unresolved.GetValueOrDefault(requestId) is { } missing ? missing with { Detail = "중계 접수 기록 없음 · 자동 재실행하지 않습니다. 기존 관제 기록을 확인하세요." } : null;
            response.EnsureSuccessStatusCode();
            var result = await response.Content.ReadFromJsonAsync<RequestRecord>(Protocol.Json, token);
            if (result is not null && result.RequestId != requestId) throw new InvalidDataException("조회 응답 ID 불일치");
            if (token.IsCancellationRequested || generation != Session.SessionGeneration) return null;
            if (result is not null) UpdatePending(result);
            return result;
        }
        catch (Exception exc) when (exc is HttpRequestException or OperationCanceledException or IOException or InvalidDataException or JsonException)
        { return unresolved.GetValueOrDefault(requestId); }
    }
    public void Close() { if (closed) return; closed = true; lifetime.Cancel(); CancelConnection(); }
    public void Dispose()
    {
        if (disposed) return;
        disposed = true; Close();
        // Allow bounded session DELETE to finish without blocking the WPF dispatcher.
        _ = (loop ?? Task.CompletedTask).ContinueWith(_ => { http.Dispose(); lifetime.Dispose(); }, TaskScheduler.Default);
    }
}
