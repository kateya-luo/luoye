# Luoye 固件 V2.2.4：离线上传内存平衡

V2.2.4 根据 V2.2.3 DMA-DIAG-R2 的现场数据，收紧离线上传的 TCP
发送缓存，降低 HTTP 与 SD/SPI 对内部 DMA 内存的瞬时争用。

## 本版调整

- TCP 发送窗口：65535 字节调整为 32768 字节。
- HTTP TX 缓冲：16 KiB 调整为 8 KiB。

## 保持不变

- TCP 接收窗口：32768 字节。
- TCP SACK：开启。
- 批量上传期间使用 `WIFI_PS_NONE`，结束后恢复原省电模式。
- SD SPI：20 MHz。
- 离线上传范围：10 MiB。
- 上传架构：单任务串行执行。
- 服务器：ClearMeeting 1.0.1，设备 API `luoye-device-api/2`。

本版未加入 DMA 水位等待、SPI 驱动补丁或额外诊断日志。
