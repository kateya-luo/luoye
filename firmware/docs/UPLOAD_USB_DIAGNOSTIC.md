# USB 离线上传诊断

本诊断只增加串口计时，不改变服务器地址、10 MiB 范围大小、重试策略或 TCP 配置。
日志不会写入 SD，也不会输出设备令牌、音频内容或 SHA 摘要。

## 测试准备

1. 使用 USB 数据线连接工程调试板。
2. 断网录制 6–8 分钟，确保产生至少一个完整 10 MiB 离线范围。
3. 恢复 Wi-Fi，进入手动同步。
4. 串口过滤 `LY|UPLOAD_DIAG`，至少保存一个 `range_begin` 到
   `range_done` 的完整序列。

## 日志字段

- `range_begin`：上传路由、RSSI、范围偏移、字节数和工作缓冲大小。
- `range_hash`：第一次 SD 读取与 SHA-256 阶段；`sd_read_ms` 是 `fread`
  累计时间，`sha_ms` 是 mbedTLS SHA 累计时间，`other_ms` 包含打开、定位和关闭文件；
  `sd_Bps` 与 `sha_Bps` 分别给出两个阶段的吞吐。
- `range_http`：第二次 SD 读取和 HTTP 流式发送；`connect_ms` 是连接及请求头阶段，
  `write_ms` 是 `esp_http_client_write` 累计阻塞时间，`response_ms` 是等待服务器响应时间。
- `effective_Bps`：从准备 HTTP 到收到响应的端到端速度。
- `write_Bps`：仅按 HTTP 写调用累计时间计算的发送速度。
- `read` 和 `sent`：发生错误时可确认失败前实际从 SD 读取和写入 TCP 的字节数。
- `range_done`：服务器确认并持久化本地上传游标后的总耗时。

计时使用 `esp_timer_get_time()`。循环内计时调用的开销远低于一次 SD 或 TCP 操作，
但诊断包仍只用于工程定位，不作为量产性能结论。
