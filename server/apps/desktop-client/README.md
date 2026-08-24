# Clear Meeting Desktop

Windows 客户端由三层组成：

- Electron：窗口、权限、服务地址和桌面生命周期。
- Web UI：实时字幕、滚动纪要、历史会议与导出。
- C# BLE Agent：后续连接 nRF52840，负责原生 Windows BLE 通信。

当前无需录音笔硬件即可选择“电脑麦克风（模拟录音笔）”，跑通：

`电脑麦克风 → PCM 16 kHz → WebSocket → FunASR → DeepSeek → 字幕/纪要`

## 开发运行

```powershell
dotnet build native/ClearMeeting.BleAgent/ClearMeeting.BleAgent.csproj -c Release
npm install
npm start
```

首次启动输入服务器地址，例如 `http://49.235.162.64` 或后续的 Tailscale 私网地址。进入实时会议页后：

1. 音频来源选择“电脑麦克风（模拟录音笔）”。
2. 选择实际麦克风。
3. 点击“开始录音”并允许麦克风权限。
4. 确认输入电平跳动、上传块数持续增加、字幕和滚动纪要出现。
5. 点击“结束会议”，确认最终纪要和会议历史保存。

## Windows 打包

```powershell
npm run pack:win
```

打包脚本会先发布 `win-x64` BLE Agent，再将其放入 Electron 的 `resources/ble-agent`。

## BLE 边界

原生 Agent 已支持扫描、连接、GATT 订阅、开始/结束会话、状态读取和字幕下发。当前电脑麦克风模式模拟的是“BLE 音频在客户端解码后的 PCM 数据”，待硬件到位后只替换音频输入适配器，云端协议和界面不需要重做。
