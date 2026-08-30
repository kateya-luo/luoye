# V1.7.1：实时 WAV 固定 DMA 写入

## 现场根因

真机在实时录音并上传约54秒后出现：

```text
sdmmc_write_sectors: not enough mem, err=0x101
LY|STORAGE_FAULT|source=pcm_short_write ... errno=5
```

卡已成功挂载、建会并持续读写，故障不是文件系统或扇区对齐错误。Newlib
隐藏的 `FILE` 写缓冲可能位于 PSRAM，ESP-IDF 通用 `sdmmc` 层因此在每次
扇区写入时临时申请512字节 DMA 缓冲；录音与 Wi-Fi 上传并行时该运行期申请
可能失败。

## 修复边界

- 仅实时录音的 `audio.wav` 句柄使用 `_IONBF`。
- 4096字节 PCM 写入数组固定为 `DMA_ATTR` 内部内存。
- 每64 KiB更新 WAV 头、提交 `committed_bytes` 和持久化范围 SHA 的节奏不变。
- 离线上传仍使用独立只读句柄和16 KiB固定读取缓冲。
- TCP、HTTP、SD 20 MHz、10 MiB范围、SHA和断点续传协议均不变。
- 存储失败日志新增内部内存、DMA总余量和最大连续 DMA 块。

## 验证要求

1. 实时录音并上传至少30分钟，不能出现 `sdmmc_write_sectors: not enough mem`。
2. 停止录音后核对 WAV 可播放、长度正确且最终 SHA 一致。
3. 断网录制长文件后恢复网络，验证10 MiB离线上传和断点续传。
4. 手动中断上传并重启，确认服务器从已确认范围继续。
