using System.Buffers.Binary;
using System.Text;

namespace ClearMeeting.BleProtocol;

public static class Protocol
{
    public const byte Version = 1;
    public const byte LiveAudioType = 0x10;

    public static readonly Guid ServiceUuid = Guid.Parse("9f4c0001-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid CommandUuid = Guid.Parse("9f4c0002-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid EventUuid = Guid.Parse("9f4c0003-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid LiveAudioUuid = Guid.Parse("9f4c0004-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid BulkDataUuid = Guid.Parse("9f4c0005-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid CaptionUuid = Guid.Parse("9f4c0006-7d2a-4a6b-b8a1-5c2e7f310001");
    public static readonly Guid StatusUuid = Guid.Parse("9f4c0007-7d2a-4a6b-b8a1-5c2e7f310001");

    public static ushort Crc16(ReadOnlySpan<byte> data)
    {
        ushort crc = 0xffff;
        foreach (var value in data)
        {
            crc ^= (ushort)(value << 8);
            for (var bit = 0; bit < 8; bit++)
                crc = (ushort)((crc & 0x8000) != 0 ? (crc << 1) ^ 0x1021 : crc << 1);
        }
        return crc;
    }

    private static byte[] Finish(byte[] packet)
    {
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(packet.Length - 2), Crc16(packet.AsSpan(0, packet.Length - 2)));
        return packet;
    }

    private static void RequireValidCrc(ReadOnlySpan<byte> packet)
    {
        if (packet.Length < 2 || BinaryPrimitives.ReadUInt16LittleEndian(packet[^2..]) != Crc16(packet[..^2]))
            throw new ProtocolException("CRC-16 mismatch");
    }

    public static byte[] EncodeAudio(AudioPacket value)
    {
        var packet = new byte[24 + value.Payload.Length + 2];
        packet[0] = Version; packet[1] = LiveAudioType; packet[2] = value.Flags; packet[3] = value.Codec;
        BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(4), value.SessionId);
        BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(12), value.FrameSequence);
        BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(16), value.TimestampMs);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(20), value.SampleCount);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(22), checked((ushort)value.Payload.Length));
        value.Payload.CopyTo(packet.AsSpan(24));
        return Finish(packet);
    }

    public static AudioPacket DecodeAudio(ReadOnlySpan<byte> packet)
    {
        if (packet.Length < 26 || packet[0] != Version || packet[1] != LiveAudioType)
            throw new ProtocolException("Invalid audio header");
        RequireValidCrc(packet);
        var payloadLength = BinaryPrimitives.ReadUInt16LittleEndian(packet[22..]);
        if (packet.Length != 24 + payloadLength + 2) throw new ProtocolException("Invalid audio length");
        return new AudioPacket(
            packet[2], packet[3], BinaryPrimitives.ReadUInt64LittleEndian(packet[4..]),
            BinaryPrimitives.ReadUInt32LittleEndian(packet[12..]), BinaryPrimitives.ReadUInt32LittleEndian(packet[16..]),
            BinaryPrimitives.ReadUInt16LittleEndian(packet[20..]), packet.Slice(24, payloadLength).ToArray());
    }

    public static byte[] EncodeCommand(CommandPacket value)
    {
        var packet = new byte[8 + value.Payload.Length + 2];
        packet[0] = Version; packet[1] = value.Opcode; packet[2] = value.Flags;
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(4), value.RequestId);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(6), checked((ushort)value.Payload.Length));
        value.Payload.CopyTo(packet.AsSpan(8));
        return Finish(packet);
    }

    public static CommandPacket DecodeCommand(ReadOnlySpan<byte> packet)
    {
        if (packet.Length < 10 || packet[0] != Version) throw new ProtocolException("Invalid command header");
        RequireValidCrc(packet);
        var payloadLength = BinaryPrimitives.ReadUInt16LittleEndian(packet[6..]);
        if (packet.Length != 8 + payloadLength + 2) throw new ProtocolException("Invalid command length");
        return new CommandPacket(packet[1], packet[2], BinaryPrimitives.ReadUInt16LittleEndian(packet[4..]), packet.Slice(8, payloadLength).ToArray());
    }

    public static byte[] EncodeCaption(CaptionPacket value)
    {
        var text = Encoding.UTF8.GetBytes(value.Text);
        var packet = new byte[22 + text.Length + 2];
        packet[0] = Version; packet[1] = value.CaptionType; packet[2] = value.Flags;
        BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(4), value.SessionId);
        BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(12), value.Revision);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(16), value.FragmentIndex);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(18), value.FragmentCount);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(20), checked((ushort)text.Length));
        text.CopyTo(packet.AsSpan(22));
        return Finish(packet);
    }

    public static CaptionPacket DecodeCaption(ReadOnlySpan<byte> packet)
    {
        if (packet.Length < 24 || packet[0] != Version) throw new ProtocolException("Invalid caption header");
        RequireValidCrc(packet);
        var textLength = BinaryPrimitives.ReadUInt16LittleEndian(packet[20..]);
        if (packet.Length != 22 + textLength + 2) throw new ProtocolException("Invalid caption length");
        return new CaptionPacket(packet[1], packet[2], BinaryPrimitives.ReadUInt64LittleEndian(packet[4..]),
            BinaryPrimitives.ReadUInt32LittleEndian(packet[12..]), BinaryPrimitives.ReadUInt16LittleEndian(packet[16..]),
            BinaryPrimitives.ReadUInt16LittleEndian(packet[18..]), Encoding.UTF8.GetString(packet.Slice(22, textLength)));
    }
}

public sealed record AudioPacket(byte Flags, byte Codec, ulong SessionId, uint FrameSequence, uint TimestampMs, ushort SampleCount, byte[] Payload);
public sealed record CommandPacket(byte Opcode, byte Flags, ushort RequestId, byte[] Payload);
public sealed record CaptionPacket(byte CaptionType, byte Flags, ulong SessionId, uint Revision, ushort FragmentIndex, ushort FragmentCount, string Text);

public sealed class ProtocolException(string message) : Exception(message);
