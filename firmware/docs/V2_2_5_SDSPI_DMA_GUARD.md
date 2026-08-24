# Luoye 固件 V2.2.5：SDSPI DMA 确定性保护

V2.2.5 保持 V2.2.4 的 32 KiB TCP 发送窗口和 8 KiB HTTP TX 缓冲，
直接修复离线上传期间 SDSPI 临时 RX DMA 分配失败导致的复位。

## 修复内容

- SDSPI 数据接收长度始终向4字节边界补齐，直接使用驱动已有的516字节
  专用 DMA block buffer，不再为509/511字节传输临时申请512字节缓冲。
- 修复 ESP-IDF 5.5.4 SPI 私有缓冲申请失败后的清理路径，失败只返回错误，
  不再从空 RX 指针复制数据。
- 每次 SD 读取前保持4 KiB最大连续 DMA 低水位；不足时暂停读取，最长等待
  30秒，让 TCP/Wi-Fi 完成发送并释放内存。
- 删除 V2.2.3 中无效的16 KiB内部 SD 暂存区，读取结果直接写入调用方的
  内部 RAM 或 PSRAM 缓冲，归还 DMA 预算并减少一次内存复制。

## 保持不变

- TCP 发送/接收窗口：32768/32768字节。
- HTTP TX：8 KiB；TCP SACK开启。
- 上传期间 `WIFI_PS_NONE`，结束后恢复原省电模式。
- SD SPI：20 MHz。
- 离线上传：10 MiB范围、单任务串行上传。
- ClearMeeting服务器1.0.1、设备API `luoye-device-api/2`。
