# ClearMeeting Windows BLE Agent

The agent owns native Windows BLE access. Electron launches it as a child process
and exchanges newline-delimited JSON over stdin/stdout.

Build:

```powershell
dotnet publish -c Release -r win-x64
```

Example input commands:

```json
{"command":"scan_start"}
{"command":"connect","address":"A1B2C3D4E5F6"}
{"command":"start_session","sessionId":"0102030405060708"}
{"command":"caption","sessionId":"0102030405060708","revision":1,"captionType":0,"flags":2,"text":"今天发布新版本"}
{"command":"stop_session","sessionId":"0102030405060708"}
{"command":"disconnect"}
```

Output uses the same newline-delimited JSON format. Live audio events contain the
decoded metadata and a Base64 codec payload. The agent does not contain cloud
credentials and does not connect to the server directly.
