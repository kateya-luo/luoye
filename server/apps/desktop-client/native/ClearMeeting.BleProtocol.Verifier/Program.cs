using System.Text;
using System.Text.Json;
using ClearMeeting.BleProtocol;

var vectorPath = args.Length == 1
    ? args[0]
    : Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", "protocol", "test_vectors.json"));
using var document = JsonDocument.Parse(File.ReadAllText(vectorPath));
var root = document.RootElement;

Assert(Protocol.Crc16(Encoding.ASCII.GetBytes("123456789")) == 0x29b1, "CRC check vector");

var audioBytes = Convert.FromHexString(root.GetProperty("audio").GetProperty("hex").GetString()!);
var audio = Protocol.DecodeAudio(audioBytes);
Assert(audio.SessionId == 0x0102030405060708 && audio.FrameSequence == 42 && audio.TimestampMs == 840, "audio fields");
Assert(Protocol.EncodeAudio(audio).SequenceEqual(audioBytes), "audio round trip");

var commandBytes = Convert.FromHexString(root.GetProperty("start_session_command").GetProperty("hex").GetString()!);
var command = Protocol.DecodeCommand(commandBytes);
Assert(command.Opcode == 0x10 && command.RequestId == 0x1234, "command fields");
Assert(Protocol.EncodeCommand(command).SequenceEqual(commandBytes), "command round trip");

var captionBytes = Convert.FromHexString(root.GetProperty("caption").GetProperty("hex").GetString()!);
var caption = Protocol.DecodeCaption(captionBytes);
Assert(caption.Text == "今天发布新版本" && caption.Revision == 7, "caption fields");
Assert(Protocol.EncodeCaption(caption).SequenceEqual(captionBytes), "caption round trip");

captionBytes[^1] ^= 0x01;
try
{
    Protocol.DecodeCaption(captionBytes);
    throw new Exception("corrupt CRC was accepted");
}
catch (ProtocolException) { }

Console.WriteLine("ClearMeeting BLE protocol V1 golden vectors: PASS");

static void Assert(bool condition, string name)
{
    if (!condition) throw new Exception($"FAILED: {name}");
}
