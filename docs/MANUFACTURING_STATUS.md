# 硬件制造归档状态

当前资料可以用于工程协作和样机回溯，但不是完整的量产制造包。

## 当前文件基线

| 类别 | 文件 | 来源日期/标识 |
| --- | --- | --- |
| PCB 工程 | `hardware/pcb/luoye-recorder-pcb-2026-07-16.epro` | 2026-07-16 |
| BOM | `hardware/pcb/luoye-recorder-bom-2026-07-10.xlsx` | 2026-07-10 |
| PCB 装配模型 | `hardware/pcb/luoye-recorder-pcb-assembly-2026-08-01.step` | 2026-08-01 |
| 外壳外部 | `hardware/mechanical/luoye-enclosure-outer-v2.step` | V2，原交接日期 2026-08-13 |
| 外壳底部 | `hardware/mechanical/luoye-enclosure-bottom-v2.step` | V2，原交接日期 2026-08-13 |
| 外壳内部 | `hardware/mechanical/luoye-enclosure-inner-v2.step` | V2，原交接日期 2026-08-13 |

PCB 工程与 BOM 日期不一致，发板前必须从最终 PCB 工程重新核对或导出 BOM。

## 投产前必须完成

- 从确认后的 PCB 工程导出 Gerber、钻孔、坐标/贴片和最终 BOM。
- 复核固件 `board_pins` 与原理图/PCB 引脚、极性和上拉配置。
- 核对充电芯片配置、电池极性、1 A 充电条件、USB 供电与热设计。
- 将 PCB 装配 STEP 与外壳 V2 做干涉检查，确认屏幕、SD、USB、按键、麦克风孔和螺柱。
- 首件完成上电、充电、录音、SD、Wi-Fi、电子墨水屏和长时间稳定性测试。
- 建立明确的 PCB 修订号、机械修订号和序列号规则。

当前仓库没有经过确认的 Gerber、钻孔和贴片坐标。因此，后续应把完整制造输出作为独立硬件 Release 归档，而不是把临时导出文件直接视为生产依据。
