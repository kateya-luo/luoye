# 硬件制造归档状态

当前资料可用于工程协作、样机展示和设计回溯，但不是完整量产制造包。ECAD、原理图 PDF 与机械 STEP 的日期和技术基线并不一致。

## 当前文件基线

| 类别 | 文件 | 日期/状态 |
| --- | --- | --- |
| 主板 ECAD | `hardware/pcb/luoye-mainboard-2026-08-30.epro` | 2026-08-30，当前工程源 |
| 双麦板 ECAD | `hardware/pcb/luoye-microphone-board-2026-08-30.epro` | 2026-08-30，当前工程源 |
| 主板原理图 | `hardware/pcb/luoye-mainboard-schematic-2026-06-21.pdf` | 2026-06-21，历史导出 |
| 双麦板原理图 | `hardware/pcb/luoye-microphone-board-schematic-2026-06-21.pdf` | 2026-06-21，历史导出 |
| 外壳/底壳/内壳 | `hardware/mechanical/luoye-enclosure-*-v2.step` | 2026-08-13 |
| 两类按钮 | `hardware/mechanical/luoye-button-*-v2.step` | 2026-08-07 |

## 已确认的基线差异

- 原理图 PDF 早于 EPRO；其中主板 PDF 仍显示 nRF52840，而当前固件目标为 ESP32-S3。
- 机械 STEP 早于最新 ECAD，尚未与 2026-08-30 板框和接口位置重新对齐。
- 仓库没有与当前 EPRO 匹配的生产 BOM、Gerber、钻孔、贴片坐标和 PCB 装配 STEP。
- 固件仍上报硬件标识 `LY-HW-ENG-20260710`；它是工程设备兼容标识，不等于 2026-08-30 ECAD 已量产锁版。

## 投产前必须完成

1. 从确认后的 2026-08-30 EPRO 重新导出原理图 PDF、BOM、Gerber、钻孔和贴片坐标。
2. 生成最新主板、麦克风板及连接器的装配 STEP，并与五个机械 STEP 联合干涉检查。
3. 复核固件 `board_pins` 与最终原理图的引脚、极性、上拉和电源域。
4. 核对充电电流、电池极性、USB 供电、热设计、屏幕接口和 SD 信号完整性。
5. 检查屏幕、USB、SD、麦克风孔、按钮行程、螺柱、电池空间和装配公差。
6. 首件完成上电、充电、录音、SD、Wi-Fi、电子墨水屏和长时间稳定性测试。
7. 建立明确的 PCB、麦克风板、机械和整机修订号，再制作独立硬件 Release。

在上述项目完成前，README 实物照片和本目录源文件均应标记为 Engineering reference，不应发送给生产方作为唯一制造依据。
