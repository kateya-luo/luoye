# 主板与麦克风板资料

| 文件 | 用途 | 状态 |
| --- | --- | --- |
| `luoye-mainboard-2026-08-30.epro` | 当前主板 ECAD 工程 | 当前工程源 |
| `luoye-microphone-board-2026-08-30.epro` | 当前双麦板 ECAD 工程 | 当前工程源 |
| `luoye-mainboard-schematic-2026-06-21.pdf` | 主板原理图预览 | 历史导出，仅供阅读 |
| `luoye-microphone-board-schematic-2026-06-21.pdf` | 双麦板原理图预览 | 历史导出，仅供阅读 |

## 已知差异

- 两份 PDF 的导出时间早于 2026-08-30 EPRO，不能视为最新工程的等价输出。
- 主板 PDF 中仍可见 nRF52840 模块，而当前固件目标是 ESP32-S3；因此 PDF 只能作为历史设计参考。
- 当前目录没有从 2026-08-30 工程重新导出的 BOM、Gerber、钻孔、贴片坐标和装配 STEP。
- 发板前必须以确认后的 EPRO 为唯一输入，重新导出并逐项核对制造文件。

不要把历史 PDF 与当前 EPRO 混合交付给生产方。
