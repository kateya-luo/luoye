# V1.7.2：SDSPI 外层 DMA 根因修复

## 根因闭环

V1.7.1 已取消实时 WAV 的 Newlib 隐藏缓冲，但 WAV 数据从44字节头部后开始，
FatFS 仍需使用扇区缓存处理非512字节对齐的首尾数据。该缓存优先位于 PSRAM，
ESP-IDF 5.5.4 通用 `sdmmc_read_sectors`/`sdmmc_write_sectors` 因而在每次传输前
临时申请512字节 DMA。实时录音与上传并行时，这个运行期申请仍可能失败。

## 修复

- 固定项目本地 ESP-IDF 5.5.4 `sdmmc` 组件。
- 仅当 `host_is_spi(card)` 且主机对齐检查通过时，直接进入 SDSPI 事务层。
- SDSPI 事务层继续把非 DMA/PSRAM 数据分块复制到永久516字节内部 DMA。
- 原生 SDMMC 主机仍执行上游的 PSRAM DMA 能力判断和临时缓冲逻辑。
- 读写两条路径使用同一个 SPI 专用判断，覆盖 FatFS数据、元数据和离线上传。

## 保持不变

SD 20 MHz、单任务上传、16 KiB读取缓冲、10 MiB范围、SHA、断点续传、
TCP/HTTP参数和服务器协议均不改变。

## 真机验收

1. 连续录音并实时上传至少2小时。
2. 断网录制后上传多个10 MiB范围，并中断、重启、续传。
3. 不得出现 `sdmmc_*_sectors: not enough mem`。
4. 核对 WAV 时长、范围 SHA 和服务器最终字节数。
