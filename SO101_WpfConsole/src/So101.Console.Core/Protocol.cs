using System.Text.Json;
using System.Text.Json.Serialization;

namespace So101.Console.Core;

public enum ConnectionState { ONLINE, COMM_LOST }
public enum RequestState { ACCEPTED, RUNNING, BUILD_DONE, PAUSED, UNKNOWN, SUCCEEDED, FAILED, CANCELLED, CLEARED }
public sealed record SessionInfo(int ProtocolVersion, string SessionGeneration, string BridgeInstanceId, bool Demo);
public sealed record CommandRequest(int ProtocolVersion, string SessionGeneration, string RequestId, string Command);
public sealed record RequestRecord(string RequestId, string Command, string Arm, CommandKind Kind,
    RequestState State, string Detail, DateTimeOffset UpdatedAt, string? ResultCode = null)
{
    [JsonIgnore] public bool Terminal => State is RequestState.SUCCEEDED or RequestState.FAILED or RequestState.CANCELLED or RequestState.CLEARED;
}
public sealed record CommandReply(int ProtocolVersion, bool Accepted, string Code, string Detail, RequestRecord? Request);
public sealed record ArmSnapshot(string Arm, ConnectionState Connection, RequestRecord? Job, RequestRecord? Control);
public sealed record Snapshot(int ProtocolVersion, string BridgeInstanceId, string SessionGeneration, long Sequence,
    bool Demo, ConnectionState BridgeConnection, DateTimeOffset ObservedAt, IReadOnlyList<ArmSnapshot> Arms, string? StorageError = null, string ActiveArm = "arm2");
public sealed record GatewayEvent(int ProtocolVersion, string BridgeInstanceId, string SessionGeneration, long Sequence,
    string EventType, DateTimeOffset OccurredAt, Snapshot Snapshot);
public sealed record SupervisionChallenge(int ProtocolVersion, string SessionGeneration, string BridgeInstanceId,
    string Arm, string Epoch, string Nonce, int ExpiresInMs);
public sealed record SupervisionAck(int ProtocolVersion, string SessionGeneration, string BridgeInstanceId,
    string Arm, string Epoch, string Nonce);

public static class Protocol
{
    public const int Version = 1;
    public static string NewId() => Guid.NewGuid().ToString("N");
    public static bool ValidId(string? value) => value is { Length: 32 } && value.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        Converters = { new JsonStringEnumConverter(allowIntegerValues: false) },
        WriteIndented = true
    };
}

/// <summary>One UI-thread consumer. The demo implementation performs no I/O.</summary>
public interface IControlGateway : IDisposable
{
    SessionInfo Session { get; }
    event Action<GatewayEvent>? Changed;
    Snapshot GetSnapshot();
    Task<CommandReply> SubmitAsync(CommandRequest request);
    Task<RequestRecord?> QueryAsync(string requestId);
    RequestRecord? Latest(bool control);
    string? UnavailableReason(string command);
    void Close();
}

public interface IDemoControls
{
    void Advance();
    void Disconnect();
    void Reconnect();
    void FailNextJob();
    void LoseNextReply();
}
