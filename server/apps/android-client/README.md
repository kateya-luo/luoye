# Clear Meeting Android V0.6

Android 原生 WebView 客户端复用云端会议页面，提供麦克风权限和文件导出桥接。推荐通过 Tailscale 私网连接服务器。

## 构建与安装

1. 在 Windows 安装 Android Studio。
2. 使用 Android Studio 打开本目录 `apps/android-client`。
3. 等待 Gradle 同步并安装 Android SDK 36。
4. 连接 Android 8.0 或更高版本的手机，启用 USB 调试。
5. 点击 Run 安装调试版；或使用 Build APK 生成安装包。
6. 首次启动输入服务器的 Tailscale 地址，例如 `http://100.64.0.10`。

重新配置服务器时，可在手机的“设置 → 应用 → Clear Meeting → 存储”中清除应用数据后再次启动。

## 当前范围

- 已实现：麦克风采集、实时转写、滚动纪要、会议历史、登录和文件导出。
- 已预留：Android 12+ BLE 扫描与连接权限。
- 下一阶段：nRF52840 录音笔扫描、配对、音频帧接收、解码和转发。
