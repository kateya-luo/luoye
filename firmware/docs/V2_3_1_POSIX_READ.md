# Luoye 固件 V2.3.1：统一 SD 定位读取

V2.3.1 是 V2.3.0 的最小存储修补版。V2.3.0 的启动日志证明 SD 卡已成功
格式化、挂载并写入 `luoye-storage/2`，但边界自检在 `offset=1, bytes=509`
处失败，导致存储子系统被降级并显示“存储不可用”。

本版本只调整读取接口：

- 文件长度统一由 `fstat(fileno(file))` 获取；
- 文件偏移统一由 `lseek(fileno(file), offset, SEEK_SET)` 设置；
- 数据统一由 `read(fileno(file), ...)` 读取；
- 上传 SHA、离线范围上传、边界自检、JSON 和待办音频不再混用
  `fseek(FILE *)` 与 `read(fd)`；
- 纯 `fseek/fwrite` 的录音写入和 WAV 修头路径保持不变。

storage/2、FAT32、SD 20 MHz、单任务上传、内存隔离和网络参数全部沿用
V2.3.0。已经初始化过的卡不需要再次格式化。
