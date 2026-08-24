using System.Text.Json;

namespace ClearMeeting.BleAgent;

internal static class JsonOutput
{
    private static readonly object Gate = new();

    public static void Send(string type, object? data = null)
    {
        lock (Gate)
        {
            Console.Out.WriteLine(JsonSerializer.Serialize(new { type, data }));
            Console.Out.Flush();
        }
    }

    public static void Error(string operation, Exception exception) =>
        Send("error", new { operation, message = exception.Message, exception = exception.GetType().Name });
}
