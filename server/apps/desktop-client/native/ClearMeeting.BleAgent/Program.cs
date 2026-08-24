using System.Globalization;
using System.Text.Json;
using ClearMeeting.BleAgent;

JsonOutput.Send("ready", new { protocol = 1, pid = Environment.ProcessId });
using var scanner = new RecorderScanner();
await using var recorder = new RecorderConnection();

while (await Console.In.ReadLineAsync() is { } line)
{
    if (string.IsNullOrWhiteSpace(line)) continue;
    try
    {
        using var document = JsonDocument.Parse(line);
        var command = document.RootElement.GetProperty("command").GetString() ?? string.Empty;
        switch (command)
        {
            case "scan_start": scanner.Start(); break;
            case "scan_stop": scanner.Stop(); break;
            case "connect":
                scanner.Stop();
                await recorder.ConnectAsync(ParseHex64(document.RootElement.GetProperty("address").GetString()));
                break;
            case "disconnect": await recorder.DisconnectAsync(); break;
            case "start_session": await recorder.StartSessionAsync(ParseHex64(document.RootElement.GetProperty("sessionId").GetString())); break;
            case "stop_session": await recorder.StopSessionAsync(ParseHex64(document.RootElement.GetProperty("sessionId").GetString())); break;
            case "caption":
                await recorder.SendCaptionAsync(
                    ParseHex64(document.RootElement.GetProperty("sessionId").GetString()),
                    document.RootElement.GetProperty("revision").GetUInt32(),
                    document.RootElement.TryGetProperty("captionType", out var captionType) ? captionType.GetByte() : (byte)0,
                    document.RootElement.TryGetProperty("flags", out var flags) ? flags.GetByte() : (byte)0x02,
                    document.RootElement.GetProperty("text").GetString() ?? string.Empty);
                break;
            case "status": await recorder.ReadStatusAsync(); break;
            case "quit": return;
            default: throw new InvalidOperationException($"Unknown command: {command}");
        }
    }
    catch (Exception exception) { JsonOutput.Error("command", exception); }
}

static ulong ParseHex64(string? value)
{
    if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException("Missing hexadecimal identifier");
    return ulong.Parse(value.Replace("0x", "", StringComparison.OrdinalIgnoreCase), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
}
