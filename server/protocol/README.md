# ClearMeeting BLE protocol implementation

- `test_vectors.json`: language-neutral golden packets.
- `generate_test_vectors.py`: deterministic vector generator.
- `../firmware/nrf52840`: C encoder/decoder for the recorder firmware.
- `../apps/desktop-client/native/ClearMeeting.BleProtocol`: C# encoder/decoder for the Windows BLE agent.

Regenerate and verify:

```powershell
python protocol/generate_test_vectors.py
dotnet run --project apps/desktop-client/native/ClearMeeting.BleProtocol.Verifier -- protocol/test_vectors.json
```

Do not change packet layouts in one implementation only. Update the protocol document, generator, golden vectors, C implementation and C# implementation in the same commit.
