# Luoye 固件 V2.3.0：存储 v2 与上传内存隔离

## 目标

V2.3.0 从空卡重新建立确定的 FAT32 存储结构，并消除离线上传期间
Wi-Fi/HTTP 动态内存挤占 SDSPI DMA 内存所导致的复位、CRC 错误和后续挂载失败。

## 根因修复

- 恢复 ESP-IDF v5.5.4 官方 SDSPI 线上传输长度；不再把 509/511 字节事务
  强行补成 512 字节，避免多块读边界错位和 `data CRC failed`。
- 仅保留 SPI DMA 临时缓冲分配失败时的安全清理保护，使内存不足返回错误，
  不再在清理路径对空指针执行复制。
- 保留 64 KiB 内部 DMA 内存，Wi-Fi/LwIP 分配优先使用 PSRAM。
- Wi-Fi 使用 16 个静态 TX、16 个静态 RX 缓冲，禁止上传突发期间无限制
  增长动态 TX 缓冲。
- FatFs 固定 512 字节逻辑扇区，文件/缓存内存优先使用 PSRAM。
- 上传保持单任务、SD 20 MHz、10 MiB 范围、32 KiB TCP 收发窗口、
  8 KiB HTTP TX、SACK 和上传期间 `WIFI_PS_NONE`。

## 存储 v2

本版本要求 SD 卡具有 `luoye-storage/2` 卡清单。旧卡第一次启动不会自动格式化，
屏幕会显示“需要初始化存储”。只有长按录音键才会执行以下破坏性操作：

1. 格式化为 FAT32，分配单元 32 KiB；
2. 创建 `/rec`、`/diag`、`/system`；
3. 写入 `/luoye-card.json`，记录 schema、卡 UUID、扇区和簇规格；
4. 写入并读回 128 KiB 测试文件；
5. 额外验证 512、509、511、516、16 KiB、64 KiB 等边界读；
6. 全部通过后自动重启。

普通 CRC、超时或一次读写失败不会触发自动格式化。格式化期间不得断电。

## 串口验收

成功初始化至少应出现：

```text
LY|STORAGE_FORMAT|event=begin filesystem=fat32 cluster=32768 destructive=1
LY|STORAGE_SELFTEST|offset=0 bytes=512 result=ok
LY|STORAGE_SELFTEST|offset=1 bytes=509 result=ok
LY|STORAGE_SELFTEST|offset=44 bytes=511 result=ok
LY|STORAGE_SELFTEST|offset=511 bytes=516 result=ok
LY|STORAGE_SELFTEST|offset=512 bytes=16384 result=ok
LY|STORAGE_SELFTEST|offset=4096 bytes=65536 result=ok
LY|STORAGE_FORMAT|event=complete schema=luoye-storage/2 result=ESP_OK
```

第一次长录音离线上传仍建议保留串口，确认无 `data CRC failed`、
`ESP_ERR_NO_MEM`、Guru Meditation 或非预期复位。
