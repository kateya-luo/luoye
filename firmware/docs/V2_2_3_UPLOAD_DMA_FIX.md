# Luoye 固件 V2.2.3：离线上传 DMA 稳定性修补

## 现场故障

V2.2.2 在离线上传第一个 10 MiB 范围时可能出现：

```text
spi_master: Failed to allocate priv RX buffer
Guru Meditation Error: Core 1 panic'ed (LoadProhibited)
```

匹配 ELF 解码后的调用链位于 `FatFS -> SDSPI -> storage_sd_read -> upload_task`。WAV 音频从文件第 44 字节开始，FatFS 会先使用逐文件扇区缓存；该缓存原先优先位于 PSRAM，迫使 SDSPI 在内部 RAM 紧张时临时申请 DMA 中转区。ESP-IDF 5.5.4 在该申请失败后的清理路径还会解引用空指针，最终导致整机重启。

## V2.2.3 修补

- 禁用 `CONFIG_FATFS_ALLOC_PREFER_EXTRAM`，让小型 FatFS 扇区缓存留在内部 RAM。
- 在创建 HTTP/TCP 大连接前打开并预热 WAV 的非对齐首扇区。
- 上传前检查内部 DMA 最大连续块；低于 8 KiB 时延后重试，不进入危险的 SDSPI 事务。
- `range_begin` 和 `range_prepare` 日志增加 `free_internal`、`largest_dma`。

## 保持不变

- SD SPI 频率仍为 20 MHz。
- 仍为单上传任务。
- TCP 发送窗口 65535、接收窗口 32768、HTTP TX 16 KiB 保持不变。
- 10 MiB 断点范围、预计算 SHA、`UPLOAD_DIAG` 和服务器 `1.0.1` 协议保持不变。

## 真机验收

离线上传时应先看到：

```text
LY|UPLOAD_DIAG|event=range_begin ... free_internal=... largest_dma=...
LY|STORAGE_DMA|event=range_prepare ... result=ready
LY|UPLOAD_DIAG|event=range_http ...
```

若内存余量不足，应出现 `result=defer` 并稍后重试，不能出现 `Guru Meditation` 或自动复位。
