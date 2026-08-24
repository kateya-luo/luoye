# Luoye 固件 V2.3.2：长录音实时上传 I/O 修复

V2.3.2 修复长时间录音时实时音频上传逐步落后、字幕后半程按单字跳动的问题。
现场 50 分 25 秒会话只实时送达约 35 分 04 秒音频；服务器处理延迟正常，瓶颈
位于固件每秒重复执行的 FAT 定位、HTTP 建连和上传状态强制落盘。

本版本保持以下稳定参数不变：

- TCP 发送/接收窗口 32768；
- HTTP TX 8192；
- SD SPI 20 MHz；
- 实时 PCM 分片 32 KiB（1 秒）；
- 离线补传 10 MiB 范围；
- 单任务云端调度。

实时路径调整如下：

- 活动会话及上传游标常驻 uploader RAM，不再每 20 ms 重读 `session.json` 和
  `upload.state`；
- `audio.wav` 每个活动会话只保留一个顺序读取句柄，只有会话切换、I/O 错误、
  离线范围补传或本地删除前才关闭；
- 上传进度每 8 片或 8 秒原子保存一次；会话创建、断网、失败、缺口、录音结束和
  模式切换仍立即保存；
- 若复位前服务器游标领先本地检查点，重发旧片后接受服务器返回的前向实时游标，
  继续使用服务器的权威进度；
- 复用同一 authenticated HTTP client 的 keep-alive 连接；传输错误后销毁并重建；
- 录音/实时上传期间使用 `WIFI_PS_NONE`，会话释放后恢复原省电模式；
- 音频落后小于 2 秒时每秒查询字幕，2～5 秒时每 2 秒查询，超过 5 秒时每 3 秒
  查询；上传仍为串行单任务。

新增串口诊断：

```text
LY|LIVE_UPLOAD_DIAG|id=... produced=... acked=... lag_bytes=... lag_ms=...
  chunks=... bytes=... sd_read_us=... sha_us=... http_us=...
  state_save_us=... checkpoint_age_ms=...
LY|LIVE_CHECKPOINT|id=... seq=... acked=... reason=... result=ESP_OK
```

建议真机连续录音 60 分钟验收：长期 `lag_ms <= 3000`，停止时待补音频不超过
96 KiB，并确认无 SD、DMA、看门狗或复位错误。已有 `luoye-storage/2` 卡无需格式化。
