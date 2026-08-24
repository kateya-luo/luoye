using ClearMeeting.BleProtocol;
using Windows.Devices.Bluetooth.Advertisement;

namespace ClearMeeting.BleAgent;

internal sealed class RecorderScanner : IDisposable
{
    private readonly BluetoothLEAdvertisementWatcher watcher = new()
    {
        ScanningMode = BluetoothLEScanningMode.Active,
    };
    private readonly HashSet<ulong> reported = [];

    public RecorderScanner() => watcher.Received += OnReceived;

    public void Start()
    {
        reported.Clear();
        watcher.Start();
        JsonOutput.Send("scan_state", new { scanning = true });
    }

    public void Stop()
    {
        if (watcher.Status is BluetoothLEAdvertisementWatcherStatus.Started or BluetoothLEAdvertisementWatcherStatus.Created)
            watcher.Stop();
        JsonOutput.Send("scan_state", new { scanning = false });
    }

    private void OnReceived(BluetoothLEAdvertisementWatcher sender, BluetoothLEAdvertisementReceivedEventArgs args)
    {
        var name = args.Advertisement.LocalName ?? string.Empty;
        var hasService = args.Advertisement.ServiceUuids.Contains(Protocol.ServiceUuid);
        if (!hasService && !name.StartsWith("ClearMeeting-", StringComparison.OrdinalIgnoreCase)) return;
        lock (reported)
        {
            if (!reported.Add(args.BluetoothAddress)) return;
        }
        JsonOutput.Send("device", new
        {
            address = args.BluetoothAddress.ToString("X12"),
            name,
            rssi = args.RawSignalStrengthInDBm,
            connectable = args.IsConnectable,
        });
    }

    public void Dispose()
    {
        Stop();
        watcher.Received -= OnReceived;
    }
}
