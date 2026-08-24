# 落叶 v0.7.0 UI154 syncqueue-sleep-r3 工程版

日期：2026-08-06

状态：`BUILD_PASS / READY_TO_FLASH`，尚待真板验收。

## 本版内容

- 网页设备存储新增“清空本地历史录音”。命令不受 30 天保留策略限制；正在写入的录音会等 WAV 安全关闭后再删除。
- 录音卡空闲 60 秒后停止 Wi-Fi，并进入 light sleep。
- light sleep 每到下一分钟唤醒一次，只读取 PCF8563、用黑白 FAST 更新主页时间，然后继续休眠；定时更新不恢复 Wi-Fi。
- REC、中间键（MARK）、BACK，以及 PCF8563 的 RTC_INT 均可唤醒；按键唤醒后恢复 Wi-Fi。
- SSD1681 编译路径只保留 1-bit 黑白全刷、1-bit 黑白 FAST 与 BUSY 等待；不包含四灰刷新函数、四灰帧缓冲或旧局刷枚举。
- uploader 任务栈保持 32 KiB，避免大批量 SD 清单和命令处理期间的栈溢出。

## 自动验证

- ESP-IDF v5.5.4-3 完整构建通过。
- 应用镜像：`1,240,448` 字节（`0x12ed80`），6 MiB factory 分区剩余约 80%。
- 服务端：`78 passed`。
- Web：`5 passed`，Vite 生产构建通过。
- 固件静态检查：manual sync/FIFO/delete、UI154、idle/light-sleep/bulk-delete/monochrome EPD 全部通过。

## 发布物

- 固件：`releases/luoye-fw-v0.7.0-ui154-r2.11-syncqueue-sleep-r3-flash.zip`
- 固件 SHA-256：`6ca3a4b2384bffbf495483b6ed6f4271927769b60fcb6f2f1528f2df7cb80606`
- 配套服务端：`clearmeeting-server-v0.13.0-syncqueue-r2.zip`
- 服务端 SHA-256：`f9fe372e12d6c25526355e992c5d863d96089b47c9ae12832e58a9f9162adba3`

## 真板待验

1. 待机 60 秒后出现 `LY|IDLE|state=enter`，路由器侧设备断开 Wi-Fi。
2. 每分钟出现 `LY|IDLE|state=minute_update`，屏幕时间 FAST 更新且 Wi-Fi 仍关闭。
3. REC、中间键、BACK 分别能唤醒，随后出现 `LY|IDLE_NET|state=resumed` 并重新联网。
4. 网页“清空本地历史录音”能删除所有已关闭本地会话；录音中的会话停止后自动执行。
5. 黑白 FAST 显示、全刷初始化和 BUSY 超时处理正常，无四灰显示路径。
