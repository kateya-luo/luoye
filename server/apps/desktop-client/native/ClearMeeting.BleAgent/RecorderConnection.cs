using System.Buffers.Binary;
using System.Runtime.InteropServices.WindowsRuntime;
using ClearMeeting.BleProtocol;
using Windows.Devices.Bluetooth;
using Windows.Devices.Bluetooth.GenericAttributeProfile;
using Windows.Security.Cryptography;
using Windows.Storage.Streams;

namespace ClearMeeting.BleAgent;

internal sealed class RecorderConnection : IAsyncDisposable
{
    private BluetoothLEDevice? device;
    private GattDeviceService? service;
    private GattCharacteristic? command;
    private GattCharacteristic? eventValue;
    private GattCharacteristic? audio;
    private GattCharacteristic? bulk;
    private GattCharacteristic? caption;
    private GattCharacteristic? status;
    private ushort requestId;

    public bool IsConnected => device?.ConnectionStatus == BluetoothConnectionStatus.Connected;

    public async Task ConnectAsync(ulong address)
    {
        await DisconnectAsync();
        device = await BluetoothLEDevice.FromBluetoothAddressAsync(address)
            ?? throw new InvalidOperationException("Bluetooth device is unavailable. Pair it in Windows Settings first if required.");
        device.ConnectionStatusChanged += OnConnectionStatusChanged;

        var services = await device.GetGattServicesForUuidAsync(Protocol.ServiceUuid, BluetoothCacheMode.Uncached);
        EnsureSuccess(services.Status, "discover recorder service");
        service = services.Services.FirstOrDefault() ?? throw new InvalidOperationException("ClearMeeting GATT service not found");

        command = await FindAsync(Protocol.CommandUuid);
        eventValue = await FindAsync(Protocol.EventUuid);
        audio = await FindAsync(Protocol.LiveAudioUuid);
        bulk = await FindAsync(Protocol.BulkDataUuid);
        caption = await FindAsync(Protocol.CaptionUuid);
        status = await FindAsync(Protocol.StatusUuid);

        eventValue.ValueChanged += OnEvent;
        audio.ValueChanged += OnAudio;
        bulk.ValueChanged += OnBulk;
        status.ValueChanged += OnStatus;

        await SubscribeAsync(eventValue, GattClientCharacteristicConfigurationDescriptorValue.Indicate);
        await SubscribeAsync(audio, GattClientCharacteristicConfigurationDescriptorValue.Notify);
        await SubscribeAsync(bulk, GattClientCharacteristicConfigurationDescriptorValue.Notify);
        await SubscribeAsync(status, GattClientCharacteristicConfigurationDescriptorValue.Notify);

        JsonOutput.Send("connection", new { connected = true, address = address.ToString("X12"), device.Name });
        await SendHelloAsync();
        await ReadStatusAsync();
    }

    public async Task DisconnectAsync()
    {
        if (eventValue is not null) eventValue.ValueChanged -= OnEvent;
        if (audio is not null) audio.ValueChanged -= OnAudio;
        if (bulk is not null) bulk.ValueChanged -= OnBulk;
        if (status is not null) status.ValueChanged -= OnStatus;
        if (device is not null) device.ConnectionStatusChanged -= OnConnectionStatusChanged;
        service?.Dispose();
        device?.Dispose();
        service = null; device = null; command = null; eventValue = null;
        audio = null; bulk = null; caption = null; status = null;
        await Task.CompletedTask;
    }

    public async Task StartSessionAsync(ulong sessionId)
    {
        var payload = new byte[22];
        BinaryPrimitives.WriteUInt64LittleEndian(payload, sessionId);
        BinaryPrimitives.WriteUInt64LittleEndian(payload.AsSpan(8), (ulong)DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        payload[16] = 1;
        BinaryPrimitives.WriteUInt32LittleEndian(payload.AsSpan(17), 16_000);
        payload[21] = 1;
        await SendCommandAsync(0x10, payload, true);
    }

    public Task StopSessionAsync(ulong sessionId, byte reason = 0)
    {
        var payload = new byte[9];
        BinaryPrimitives.WriteUInt64LittleEndian(payload, sessionId);
        payload[8] = reason;
        return SendCommandAsync(0x11, payload, true);
    }

    public async Task SendCaptionAsync(ulong sessionId, uint revision, byte captionType, byte flags, string text)
    {
        Require(caption, "caption");
        var utf8 = System.Text.Encoding.UTF8.GetBytes(text);
        const int maxFragmentBytes = 180;
        var count = Math.Max(1, (utf8.Length + maxFragmentBytes - 1) / maxFragmentBytes);
        for (var index = 0; index < count; index++)
        {
            var start = index * maxFragmentBytes;
            var length = Math.Min(maxFragmentBytes, utf8.Length - start);
            // Fragment bytes may split UTF-8 code points, so build the packet directly rather than round-tripping the string.
            var packet = EncodeCaptionBytes(captionType, flags, sessionId, revision, (ushort)index, (ushort)count,
                utf8.AsSpan(start, Math.Max(0, length)));
            var option = captionType == 1 || revision % 10 == 0
                ? GattWriteOption.WriteWithResponse : GattWriteOption.WriteWithoutResponse;
            await WriteAsync(caption!, packet, option, "caption");
        }
    }

    public async Task ReadStatusAsync()
    {
        Require(status, "status");
        var result = await status!.ReadValueAsync(BluetoothCacheMode.Uncached);
        EnsureSuccess(result.Status, "read status");
        EmitRaw("status", BufferBytes(result.Value));
    }

    private async Task SendHelloAsync()
    {
        var payload = new byte[6];
        payload[0] = 1; payload[1] = 1;
        BinaryPrimitives.WriteUInt32LittleEndian(payload.AsSpan(2), 0x000000ff);
        await SendCommandAsync(0x01, payload, true);
    }

    private async Task SendCommandAsync(byte opcode, byte[] payload, bool response)
    {
        Require(command, "command");
        requestId++;
        if (requestId == 0) requestId++;
        var packet = Protocol.EncodeCommand(new CommandPacket(opcode, 0, requestId, payload));
        await WriteAsync(command!, packet, response ? GattWriteOption.WriteWithResponse : GattWriteOption.WriteWithoutResponse, "command");
    }

    private async Task<GattCharacteristic> FindAsync(Guid uuid)
    {
        var result = await service!.GetCharacteristicsForUuidAsync(uuid, BluetoothCacheMode.Uncached);
        EnsureSuccess(result.Status, $"discover {uuid}");
        return result.Characteristics.FirstOrDefault() ?? throw new InvalidOperationException($"Characteristic {uuid} not found");
    }

    private static async Task SubscribeAsync(GattCharacteristic value, GattClientCharacteristicConfigurationDescriptorValue mode)
    {
        var status = await value.WriteClientCharacteristicConfigurationDescriptorAsync(mode);
        EnsureSuccess(status, $"subscribe {value.Uuid}");
    }

    private static async Task WriteAsync(GattCharacteristic value, byte[] bytes, GattWriteOption option, string operation)
    {
        var result = await value.WriteValueWithResultAsync(CryptographicBuffer.CreateFromByteArray(bytes), option);
        EnsureSuccess(result.Status, operation);
    }

    private void OnConnectionStatusChanged(BluetoothLEDevice sender, object args) =>
        JsonOutput.Send("connection", new { connected = sender.ConnectionStatus == BluetoothConnectionStatus.Connected, sender.Name });

    private static void OnEvent(GattCharacteristic sender, GattValueChangedEventArgs args) => EmitRaw("event", BufferBytes(args.CharacteristicValue));
    private static void OnBulk(GattCharacteristic sender, GattValueChangedEventArgs args) => EmitRaw("bulk", BufferBytes(args.CharacteristicValue));
    private static void OnStatus(GattCharacteristic sender, GattValueChangedEventArgs args) => EmitRaw("status", BufferBytes(args.CharacteristicValue));

    private static void OnAudio(GattCharacteristic sender, GattValueChangedEventArgs args)
    {
        try
        {
            var bytes = BufferBytes(args.CharacteristicValue);
            var frame = Protocol.DecodeAudio(bytes);
            JsonOutput.Send("audio", new
            {
                sessionId = frame.SessionId.ToString("X16"),
                frameSeq = frame.FrameSequence,
                timestampMs = frame.TimestampMs,
                flags = frame.Flags,
                codec = frame.Codec,
                sampleCount = frame.SampleCount,
                payload = Convert.ToBase64String(frame.Payload),
            });
        }
        catch (Exception exception) { JsonOutput.Error("decode_audio", exception); }
    }

    private static byte[] BufferBytes(IBuffer buffer)
    {
        CryptographicBuffer.CopyToByteArray(buffer, out var bytes);
        return bytes;
    }

    private static void EmitRaw(string type, byte[] bytes) => JsonOutput.Send(type, new { hex = Convert.ToHexString(bytes).ToLowerInvariant() });

    private static void EnsureSuccess(GattCommunicationStatus status, string operation)
    {
        if (status != GattCommunicationStatus.Success) throw new InvalidOperationException($"{operation}: {status}");
    }

    private static void Require(object? value, string name)
    {
        if (value is null) throw new InvalidOperationException($"Not connected: {name} characteristic unavailable");
    }

    private static byte[] EncodeCaptionBytes(byte type, byte flags, ulong sessionId, uint revision,
        ushort fragmentIndex, ushort fragmentCount, ReadOnlySpan<byte> text)
    {
        var packet = new byte[22 + text.Length + 2];
        packet[0] = 1; packet[1] = type; packet[2] = flags;
        BinaryPrimitives.WriteUInt64LittleEndian(packet.AsSpan(4), sessionId);
        BinaryPrimitives.WriteUInt32LittleEndian(packet.AsSpan(12), revision);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(16), fragmentIndex);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(18), fragmentCount);
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(20), checked((ushort)text.Length));
        text.CopyTo(packet.AsSpan(22));
        BinaryPrimitives.WriteUInt16LittleEndian(packet.AsSpan(packet.Length - 2), Protocol.Crc16(packet.AsSpan(0, packet.Length - 2)));
        return packet;
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync();
}
