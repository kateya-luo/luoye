# 落叶（Luoye）录音卡固件进度日志

> 更新时间：2026-08-14
> 当前活动版本：`v0.9.9-engineering-timeline-font-r1`（`0.9.9`）
> 当前阶段：`BUILD_PASS / READY_TO_FLASH`

## 1. 状态说明

| 状态 | 含义 |
|---|---|
| PLANNED | 范围已定义，尚未编码 |
| IN_PROGRESS | 正在编码或修复 |
| BLOCKED | 被明确的外部依赖阻塞 |
| CODE_COMPLETE | 代码范围完成，尚未完成构建 |
| BUILD_PASS | 干净构建和自动测试通过 |
| READY_TO_FLASH | 产物与清单齐全，可烧录 |
| FLASHED | 已烧录，尚未完成真板验收 |
| BOARD_PASS | 真板专项测试通过 |
| ACCEPTED | 已归档并可作为下一版本基线 |

## 2. 当前基线事实

正式固件目录：

```text
D:\OPENOP\recorder-card-hw-test\firmware\recorder-card
```

当前工程情况：

- Git：已初始化；当前开发分支 `codex/luoye-api-v1`
- baseline commit：`ed04e41de642be1fecb1391b9f78d77e5c8dfd32`
- baseline tag：`luoye-fw-v0.1.0-baseline`
- v0.1.1 RC commit：`120f90404b7bfd905ec0ea7a52687bea861a5ccb`
- v0.1.1 RC tag：`luoye-fw-v0.1.1-engineering-rc.1`
- v0.1.1 CMake 项目版本：`0.1.1-engineering`
- UI 版本来源：ESP app descriptor（不再手写第二份版本）
- 目标芯片：ESP32-S3
- Flash：16 MiB
- PSRAM：8 MiB
- 当前分区：factory 6 MiB + assets 8 MiB，**没有 OTA 分区**
- 最近观察到的新板串口：COM22（每次烧录仍需重新确认）
- 当前状态机回归结果：通过，72 次渲染调用
- 已有真板证据：I²C、墨水屏显示、按键、双麦、SD、WiFi 基本通过
- v0.1.1 RC 尚缺：正式烧录日志和真板验收

Git 初始化前的构建产物（历史证据，不能作为发布包）：

| 文件 | 大小 | 时间 | SHA-256 |
|---|---:|---|---|
| `recorder_card.bin` | 1,078,832 B | 2026-07-30 11:48:20 | `A42A38888F5E877A304BA5C98326831EFD96739778E20B1B130444E3CA573C02` |
| `bootloader.bin` | 22,272 B | 2026-07-14 18:11:19 | `B558981B61876F17DECA1679D607D99DFC3BE35071E02DE270CCFC993837D21B` |
| `partition-table.bin` | 3,072 B | 2026-07-14 18:08:56 | `790DA65D2865060DEAA60622AC02D0E47FF454DDB426729351593934AD9ACC45` |
| `assets.bin` | 8,388,608 B | 2026-07-30 11:48:02 | `8DD07AAAE9BE6569F8158CB7637530FAE79A0A4AF350B2767196B8B35C9BB4CB` |

已标记的 `v0.1.0` 源码/构建 baseline：

| 文件 | SHA-256 |
|---|---|
| `recorder_card.bin` | `179FBC57E0DB2754DF305E1BC397318A8B9ED0D5AB624828663696DF221B9B6B` |
| `bootloader.bin` | `C5B50333900677D76E678F27AA5E35969F8F8ABEFE0EA9CCAD02869F99C1720F` |
| `partition-table.bin` | `790DA65D2865060DEAA60622AC02D0E47FF454DDB426729351593934AD9ACC45` |
| `assets.bin` | `8DD07AAAE9BE6569F8158CB7637530FAE79A0A4AF350B2767196B8B35C9BB4CB` |

该 baseline 已通过 PC 回归和干净构建，但未完成正式固件真板验收，因此状态是
`BUILD_PASS`，不是 `ACCEPTED`。

## 3. 版本总进度

| 版本 | 目标 | 状态 | 构建 | 烧录 | 真板 | 结论 |
|---|---|---|---|---|---|---|
| `v0.1.0-baseline` | 冻结当前基线 | BUILD_PASS | 通过 | 未执行 | 待记录 | 源码/构建已冻结 |
| `v0.1.1-engineering` | 版本、日志、测试工程化 | BOARD_PASS | RC.1 通过 | COM22 成功 | 用户确认功能通过 | 待 20 次复位后 ACCEPTED |
| `v0.2.0-storage-safe` | 可靠录音与安全收尾 | READY_TO_FLASH | dev.2 通过 | dev.1 已烧录 | dev.1 栈溢出 | dev.2 待真板 |
| `v0.3.0-provisioning` | SoftAP 配网和账号绑定 | READY_TO_FLASH / SERVER_BLOCKED | dev.1 通过 | 未执行 | 待验证 | SoftAP 包就绪，服务器 API 缺失 |
| `v0.4.0-cloud-sync` | 持久离线补传 | READY_TO_FLASH / SERVER_BLOCKED | dev.1 通过 | 未执行 | 待验证 | 设备包已冻结，服务器 API 缺失 |
| `v0.5.0-live-ai-ui` | 字幕、翻译、HTML V2 | READY_TO_FLASH / SERVER_BLOCKED | dev.1 通过 | 未执行 | 待验证 | dev.1 已冻结，服务器 0.4 API 缺失 |
| `v0.6.0-agenda-todo` | 议程、提醒、语音待办 | READY_TO_FLASH / SERVER_BLOCKED | RC 全量构建通过 | 未执行 | 待验证 | 工程包已冻结，云端依赖 0.5-draft |
| `v0.6.1-cloud-v1` | 三端 API v1 收敛 | BUILD_PASS / API_V1_INTEGRATION | 6 组主机测试、4 组静态门禁、clean build 通过 | 未执行 | 待验证 | 不打包、不烧录；等待三端端到端联调 |
| `v0.7.0-power-recovery` | 电源、恢复、长稳、固定 SD 管理 | BUILD_PASS | 主机测试、静态门禁、ESP-IDF dev build | 待执行 | 待验证 | 与 ClearMeeting 0.13.0 成套部署 |
| `v0.8.0-ota-security` | OTA 与安全加固 | PLANNED | — | — | — | — |
| `v0.9.0-rc` | 发布候选 | PLANNED | — | — | — | — |
| `v1.0.0` | 正式发布 | PLANNED | — | — | — | — |

## 4. Baseline 收尾与当前活动版本

### v0.1.0-baseline

### 范围

- [x] 初始化正式固件 Git 仓库。
- [x] 增加适合 ESP-IDF 的 `.gitignore`。
- [x] 固定 ESP-IDF v5.5.4 构建证据。
- [x] 清理并重新生成 build。
- [x] 重新运行状态机测试。
- [ ] 生成 baseline manifest 和 SHA-256。
- [ ] 确认开发板串口。
- [ ] 烧录 baseline 候选版本。
- [ ] 保存完整串口日志。
- [ ] 执行通用硬件冒烟测试。
- [ ] 记录所有已知限制。
- [x] 标记 `luoye-fw-v0.1.0-baseline`。

### 不在本版本范围

- SoftAP 配网
- 账号绑定
- 可靠离线补传
- 字幕和翻译
- 议程和语音待办
- OTA 和 eFuse 安全配置

### 完成标准

- 当前源码可以从干净环境重复构建。
- 烧录后的版本、commit 和归档 manifest 完全一致。
- 现有按键、LED、屏幕、录音、SD 和 I²C 行为无回归。
- 上述证据全部记录后，状态才从 `BOARD_PASS` 改为 `ACCEPTED`。

### v0.1.1-engineering

- [x] 统一内部产品名为落叶 / Luoye。
- [x] CMake 单一注入 `PROJECT_VER`、commit、dirty、硬件版本和构建类型。
- [x] UI 改为读取 ESP app descriptor 版本。
- [x] 增加 `LY|BOOT / LY|INIT / LY|BOOT_READY` 结构化日志。
- [x] 增加 `LYE-xxx` 错误码和子系统状态快照。
- [x] 事件队列投递失败计数并限频报警。
- [x] 固化 dev / rc / release 构建配置。
- [x] 建立 API draft、session manifest draft 和测试向量。
- [x] 建立 release manifest 与 SHA-256 打包脚本。
- [x] 在正式分支提交精确源码。
- [x] 运行 PC 回归、RC fullclean 构建和打包。
- [x] 生成 `luoye-fw-v0.1.1-engineering-rc.1`。
- [x] 确认串口并烧录 RC.1。
- [x] 确认“按键后屏幕不更新”由接错屏幕造成，RC.1 无固件缺陷。
- [x] 撤销 RC.2 代码并删除 RC.2 标签、发布包和生成目录。
- [x] 用户确认 RC.1 墨水屏、按键、录音、标记、暂停/继续和收尾功能正常。
- [ ] 保存 20 次启动和整机冒烟测试证据。

## 5. 当前阻塞项

| ID | 阻塞项 | 影响版本 | 解除条件 | 状态 |
|---|---|---|---|---|
| B-001 | 线上服务器源码比本地源码更新 | v0.3+ | 导出线上准确源码、DB schema、配置和迁移 | CLOSED：已取得统一源码基线 |
| B-002 | 设备 API 请求/响应未冻结 | v0.3+ | 三端实现 `luoye-device-api/1` | CLOSED：2026-08-01 契约冻结 |
| B-003 | 新 PCB RTC_INT 实际引脚尚未同步到正式 `board_pins.h` | v0.7 | 核对网表并真板验证深睡唤醒 | OPEN |
| B-004 | 当前工程没有 Git/版本注入 | 全部版本 | 完成 v0.1.1-engineering | CLOSED |
| B-005 | 新板当前未连接，系统仅见蓝牙 COM3/COM4 | v0.1.1 | 重新连接新板并识别 Espressif 串口 | CLOSED |

## 6. 风险台账

| ID | 风险 | 严重度 | 应对版本 | 状态 |
|---|---|---:|---|---|
| R-001 | SD 写失败时界面仍显示录音 | P0 | v0.2 | OPEN |
| R-002 | 收尾 3.2 秒不等待文件真正闭合 | P0 | v0.2 | OPEN |
| R-003 | 重启/新会话使旧上传进度永久丢失 | P0 | v0.4 | OPEN |
| R-004 | 默认 HTTP 明文传输音频和 Token | P0 | v0.3/v0.4 | OPEN |
| R-005 | 设备 ID 只有 MAC 后四位，可能碰撞 | P0 | v0.3 | OPEN |
| R-006 | 线上和本地后端源码不一致 | P0 | v0.3 Gate | OPEN |
| R-007 | 账号换绑可能错误转移旧 backlog | P0 | v0.3/v0.4 | OPEN |
| R-008 | RTC_INT 仍配置为 GPIO41，不能深睡唤醒 | P1/P0* | v0.7 | OPEN |
| R-009 | 单 factory 分区，无 OTA/回滚 | P1 | v0.8 | OPEN |
| R-010 | Secure Boot/Flash/NVS encryption 未启用 | P1 | v0.8 | OPEN |
| R-011 | 当前中文字体仅允许开发原型使用，可能阻断量产分发 | P1 | v0.8 | OPEN |
| R-012 | 曾误判 GDEY0213B74 深睡后刷新失败 | P1 | v0.1.1 | CLOSED：根因为接错屏幕，RC.2 已撤销 |

`*`：若“关机后到点提醒”是必须承诺，则 R-008 为 P0。

## 7. 工作日志

### 2026-07-30 — 规划与基线审计

**版本**：`v0.1.0-baseline`

**完成内容**：

- 完成 HTML 交互、正式固件、服务器和线上接口的交叉审计。
- 固定 WiFi 直传、SD 优先、单账号拥有者、异步云端处理等产品规则。
- 形成版本路线图、发布门禁和进度日志。
- 记录现有 build 产物大小与 SHA-256。

**发现问题**：

- 正式固件目录尚无 Git。
- 当前项目没有 CMake 版本信息。
- 线上服务器设备路由存在，但对应源码不在本地仓库。
- 当前 build 不是经过本门禁重新产生的 accepted 基线。

**下一步**：

1. 在正式固件目录建立源码版本基线。
2. 固定 ESP-IDF 版本并执行干净构建。
3. 生成 `v0.1.0-baseline-rc.1` 产物。
4. 经用户确认串口后烧录并保存日志。
5. 真板通过后标记 baseline accepted，再开始 `v0.1.1-engineering`。

### 2026-07-30 21:08 — 启动落叶 v0.1.1-engineering

**版本**：`v0.1.1-engineering`
**阶段变化**：`PLANNED → IN_PROGRESS`
**证据等级**：本地源码与构建确认

**已完成**：

- 正式目录初始化 Git，冻结 commit `ed04e41de642be1fecb1391b9f78d77e5c8dfd32`。
- 建立 annotated tag `luoye-fw-v0.1.0-baseline`。
- baseline 状态机测试通过，ESP-IDF v5.5.4 干净构建通过。
- 当前工作分支切换为 `codex/luoye-v0.1.1-engineering`。
- 设计单一版本源、启动诊断、统一错误码、构建配置和归档门禁。

**决定**：

- `v0.1.0` 保留为不可移动的源码/构建锚点。
- `v0.1.1-engineering` 是第一份准备正式烧录验收的“落叶工程 baseline”。
- 暂定硬件标识为 `LY-HW-ENG-20260710`；量产前必须换成正式 PCB revision。

**未完成**：

- v0.1.1 代码提交、RC 干净构建和发布包。
- COM 口确认、烧录、20 次启动一致性和整机真板验收。

### 2026-07-30 21:41 — 落叶 v0.1.1 RC.1 进入 READY_TO_FLASH

**版本**：`v0.1.1-engineering`
**阶段变化**：`IN_PROGRESS → CODE_COMPLETE → BUILD_PASS → READY_TO_FLASH`
**证据等级**：本地精确源码、干净构建、归档解包校验

**版本组合**：

- 固件 commit：`120f90404b7bfd905ec0ea7a52687bea861a5ccb`
- annotated tag：`luoye-fw-v0.1.1-engineering-rc.1`
- 硬件标识：`LY-HW-ENG-20260710`
- 构建类型：`rc`
- API contract：`luoye-device-api/0.1-draft`
- ESP-IDF：`v5.5.4`

**代码修改**：

- 建立 Luoye 单一版本源、commit/dirty/硬件/构建类型注入。
- 增加 `LY|BOOT`、`LY|INIT`、`LY|BOOT_READY`、`LYE-xxx` 诊断。
- 让各硬件初始化返回真实错误；事件队列失败可观测。
- 建立 dev/rc/release 构建配置、归档清单、SHA-256 和 API/测试文档。
- 构建入口每次先 reconfigure，防止增量构建沿用旧 Git 身份。
- 打包器兼容 Windows PowerShell 5.1、CRLF 和 UTF-8 产品名。

**自动测试**：

- 状态机/界面回归：全部通过，43 次渲染调用。
- PowerShell 脚本语法：通过。
- 发布包：压缩后解包，`SHA256SUMS.txt` 逐文件复验通过。

**构建**：

- Git dirty：`false`
- 内嵌 commit：`120f90404b7b`
- App 大小：`0x100640`（1,050,176 B），app 分区剩余 83%。
- `bootloader.bin`：`1888c9d31f5ded5c33fccee31747ce5b191c3ba335ec5a47257ce1ef25e0ec84`
- `partition-table.bin`：`790da65d2865060deaa60622ac02d0e47ff454ddb426729351593934ad9acc45`
- `recorder_card.bin`：`80a0f32ad57b56adbf65db33c8caedcbaaee154943acff4f84c9b9903e624dac`
- `assets.bin`：`8dd07aaae9be6569f8158cb7637530fae79a0a4af350b2767196b8b35c9bb4cb`

**归档**：

- Flash ZIP SHA-256：`682746d00936a310a7a9c78ccf9d5bd5775a6ba8063ea6d62cf6ec23df9608ca`
- Symbols ZIP SHA-256：`a80abeef4cc65793ee8d55acc2dc94993e33ff826ec8a910f1f4f0667ef5be6b`
- 烧录偏移：`0x0` bootloader、`0x8000` partition、`0x10000` app、`0x610000` assets。
- 清单确认：产品中文名“落叶”、版本、commit、硬件标识和四个偏移均正确。

**烧录**：

- 2026-07-30 21:41 扫描仅发现蓝牙 COM3/COM4。
- 未发现 COM22 或其他 Espressif USB 串口；为避免误烧，未执行 flash。

**下一步**：

1. 连接新板并重新识别 Espressif 串口。
2. 仅烧录 tag `luoye-fw-v0.1.1-engineering-rc.1` 对应包。
3. 保存启动日志并核对 `LY|BOOT` 的 version/commit/dirty/profile。
4. 执行 20 次启动一致性、按键、LED、墨水屏、录音、SD、I²C 和 WiFi 冒烟测试。

### 2026-07-30 21:57 — RC.1 已烧录至新板

**版本**：`v0.1.1-engineering`
**阶段变化**：`READY_TO_FLASH → FLASHED`
**证据等级**：COM22 真板启动日志

**烧录结果**：

- 四个镜像均完成写入并显示 `Hash of data verified`。
- 烧录端口：COM22。
- 未执行整片 `erase_flash`，仅擦写发布清单中的四个区域。

**启动身份**：

- version：`0.1.1-engineering`
- commit：`120f90404b7b`
- dirty：`0`
- hardware：`LY-HW-ENG-20260710`
- flavor：`rc`
- ESP-IDF：`v5.5.4`
- reset：`POWERON`

**初始化结果**：

- `nvs / event_queue / timers / led / ui / keys / power_i2c / storage_sd / audio_pdm / network` 全部 `status=OK`。
- `LY|BOOT_READY`：`ok=0x000003ff degraded=0x00000000 failed=0x00000000 event_drops=0`。
- SD 挂载成功，容量约 15,193 MB。
- PDM：2.048 MHz、16 kHz 双声道、软件增益 8x。
- WiFi 无凭据，按设计保持离线，不影响 SD 录音。

**观察项**：

- ESP-IDF 输出 I²C 上拉检查提示，但 `power_i2c=ESP_OK`，当前不是失败。
- SD 初始化阶段的 CMD52/CMD5 “not supported” 是 SPI 模式探测过程；随后挂载成功。

**下一步**：

1. 完成 LED、按键、墨水屏方向与内容的人工检查。
2. 录制并回放左右声道 WAV，检查增益、底噪和远场效果。
3. 设置 WiFi 凭据并验证配网流程。
4. 完成 20 次复位启动一致性后，才进入 `BOARD_PASS`。

### 2026-07-30 22:13 — RC.2 临时修复尝试（已撤销）

> 2026-07-30 22:22 更正：后续确认根因是接错屏幕，RC.1 固件正常。
> 本节仅保留审计过程；RC.2 标签、发布包、生成目录和代码修改均已撤销，不得烧录。

**版本**：`v0.1.1-engineering`
**阶段变化**：`FLASHED（RC.1）→ READY_TO_FLASH（RC.2）`
**证据等级**：RC.1 COM22 真板复现 + RC.2 本地全量构建/归档校验

**RC.1 复现**：

- 短按 REC 后红灯常亮。
- 串口出现 `LY|REC|action=start result=ok`。
- 墨水屏仍停留在待机首帧，没有进入录音页面。
- 结论：按键、事件队列和录音状态机正常；故障位于 EPD 后续刷新路径。

**RC.2 修改**：

- GDEY0213B74 从 deep sleep 唤醒前，循环一次屏幕外部电源，再执行硬件复位。
- FAST/PART 暂时统一降级为已验证的全刷路径。
- 首次发生降级时输出 `LY|UI|safe_refresh=full` 诊断日志。
- 代价：页面刷新更慢且会闪屏；快刷在单独完成真板波形验证后再恢复。

**构建与归档**：

- commit：`3cd50ef489446f42a5f9d83a1bc7f2c8cabe85cd`
- tag：`luoye-fw-v0.1.1-engineering-rc.2`
- 状态机/界面回归：全部通过，43 次渲染调用。
- RC.2 全量构建：通过。
- App：1,049,136 B，SHA-256 `2645cbf4eaa1dbdb627bee00f46e81a30a484e4cbb071ab1f4a38b1c6effdefb`
- Flash ZIP SHA-256：`77f42425f99aec4d4631c59eaa92a029dee0ccdb66d671d3d35180fd73aa11ec`
- Symbols ZIP SHA-256：`f453a7e70891160e5f994ba835144cacc72cbe0f1febfaf9b90309b27a8cf744`

**当时计划（已作废）**：

- 原计划烧录 RC.2 验证刷新；根因更正后取消，RC.2 未烧录。

### 2026-07-30 22:22 — 撤销 RC.2，RC.1 进入 BOARD_PASS

**版本**：`v0.1.1-engineering`
**阶段变化**：`READY_TO_FLASH（RC.2）→ BOARD_PASS（RC.1）`
**证据等级**：用户真板更正与功能确认

**更正结论**：

- RC.1 的按键、事件、录音和 GDEY0213B74 刷新逻辑均正常。
- 此前“按键后屏幕不更新”由接错屏幕造成，不是 RC.1 固件问题。
- 用户确认墨水屏页面切换、按键、录音、MARK、暂停/继续、结束收尾及其余检查功能正常。

**RC.2 清理**：

- 删除 annotated tag `luoye-fw-v0.1.1-engineering-rc.2`。
- 删除 RC.2 Flash/Symbols ZIP 及两个 SHA-256 sidecar。
- 删除生成目录 `build-rc2`。
- commit `e4fe7b0` 通过 Git revert 撤销 RC.2 的 EPD/UI 代码，不改写审计历史。
- RC.1 tag `luoye-fw-v0.1.1-engineering-rc.1` 与四个发布文件保持不变。

**当前结论**：

- RC.1 状态：`BOARD_PASS`。
- 尚未执行 20 次复位启动一致性，因此暂不进入 `ACCEPTED`。
- 后续所有烧录继续使用 RC.1，不得使用已撤销的 RC.2。

## 8. 后续日志模板

### 2026-07-30 22:30 — 启动 v0.2.0 可靠录音与断电恢复

**版本**：`0.2.0-dev.1`
**阶段变化**：PLANNED → IN_PROGRESS
**目标**：SD 可靠录音、安全收尾、录音中断电后的启动修复。

**代码修改**：

- 新增 `STARTING`、`CLOSING`、`STORAGE_ERROR` 三个真实状态。
- 屏幕仅在 `APP_EV_SESSION_CLOSE_DONE` 后显示“本地已保存”。
- 会话 ID 改为设备 MAC + NVS 单调计数器 + 随机后缀。
- 每段录音创建 `session.json`、`audio.wav`、`marks.jsonl`、`upload.state`。
- 所有写入、刷新、同步和关闭返回值进入统一错误处理。
- 启动扫描未闭合会话，按实际文件长度修复 WAV 头和 JSONL 尾部。
- 增加音频环形缓冲丢弃样本与溢出次数统计。

**自动测试**：

- `tools\run_state_test.bat`：通过。
- `tools\run_storage_test.bat`：通过。
- 覆盖“未收到关闭完成不能显示已保存”、写卡失败、关闭同步失败、
  低电安全收尾、WAV 奇数尾字节和 JSONL 半条记录。

**下一步**：

- 完成 ESP-IDF v5.5.4 干净构建并修复编译差异。
- 生成 `0.2.0-dev.1` 工程烧录包。
- 真板依次执行拔卡、满卡、录音中复位和 2 小时连续录音。

### 2026-07-30 23:10 — v0.2.0-dev.1 首次构建通过

**版本**：`0.2.0-dev.1`
**阶段变化**：IN_PROGRESS → BUILD_PASS

**自动测试**：

- 状态机回归：通过，40 次渲染请求。
- 存储格式与断电尾部测试：通过。
- `git diff --check`：通过。

**构建**：

- ESP-IDF：`v5.5.4`
- Target：`esp32s3`
- Profile：`dev`
- App 大小：约 1.11 MiB，factory 分区余量约 82%。
- 结果：完整构建和加固后的增量构建均通过。

**风险边界**：

- 当前 `BUILD_PASS` 只证明代码和构建通过，不等于 SD 真板故障注入通过。
- 未对用户板上的 `v0.1.1-engineering-rc.1` 执行覆盖烧录。
- 生成开发烧录包后仍需验证拔卡、满卡、录音中复位和长录音。

### 2026-07-30 23:20 — v0.2.0-dev.1 开发包冻结

**版本**：`0.2.0-dev.1`
**阶段变化**：BUILD_PASS → READY_TO_FLASH

**版本组合**：

- 源码 commit：`2231aadc0b3aee7e020c52325afab3443dc5c773`
- annotated tag：`luoye-fw-v0.2.0-dev.1`
- 构建 profile：`dev`
- 固件 manifest：`dirty=false`，target=`esp32s3`，Flash 文件数=`4`

**发布产物**：

- `releases\luoye-fw-v0.2.0-dev.1-flash.zip`
- Flash ZIP SHA-256：
  `5dec6e3162317fe09c68d3ae7a3307f39ddd788a1cf5be643b74275abda8cc2f`
- `releases\luoye-fw-v0.2.0-dev.1-symbols.zip`
- Symbols ZIP SHA-256：
  `210841aad2058f19bd35dc9257d610e27b3a60041f2042dcf3e835c80398b134`

**结论**：

- 可以烧入专用测试板开始 SD 故障注入。
- 该包尚未标记 `BOARD_PASS`，不得替代已确认正常的 RC.1 生产基线。

### 2026-07-30 23:35 — 撤销 v0.2.0-dev.1

**版本**：`0.2.0-dev.1`
**阶段变化**：READY_TO_FLASH → REVOKED

**真板证据**：

- app descriptor、commit 和 ELF 均与 dev.1 manifest 一致。
- 启动到 `LY|INIT|subsystem=led status=OK` 后触发
  `A stack overflow in task main has been detected`。
- Backtrace 已用 dev.1 ELF 符号化，确认由
  `vApplicationStackOverflowHook` 触发；不是 SD 读写错误。
- 构建配置 `CONFIG_ESP_MAIN_TASK_STACK_SIZE=3584`，UI/文件系统初始化调用链
  已超过该余量。

**修复决策**：

- dev.1 标签保留用于追踪，但烧录包不得继续使用。
- 新版本号为 `0.2.0-dev.2`，不覆盖、复用 dev.1 产物。
- 主任务栈提高到 8192 字节。
- 在 boot、UI 前后、storage 后和 boot-ready 输出主任务最小剩余栈水位。

**下一步**：

- 构建 dev.2 并确认固件描述中的版本和新栈配置。
- 重新烧录真板，要求日志完整到 `LY|BOOT_READY` 和
  `LY|STACK|task=main stage=boot_ready`。

### 2026-07-30 23:55 — v0.2.0-dev.2 修复包冻结

**版本**：`0.2.0-dev.2`
**阶段变化**：IN_PROGRESS → BUILD_PASS → READY_TO_FLASH

**修复内容**：
- 将 `CONFIG_ESP_MAIN_TASK_STACK_SIZE` 从 3584 提高到 8192 字节。
- 在 boot、UI 前后、storage 后和 boot-ready 输出 `LY|STACK` 主任务栈水位。
- `v0.2.0-dev.1` 保留标签用于追溯，但已撤销，禁止继续烧录。

**自动测试与构建**：
- 状态机回归：通过，40 次渲染请求。
- 存储格式与断电尾部测试：通过。
- ESP-IDF `v5.5.4`、target `esp32s3`、dev profile 干净构建：通过。
- 生成配置确认：`CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192`。

**版本组合**：
- 源码 commit：`01d0d367dee5816ce1088e288d5bad7e7d754e22`
- annotated tag：`luoye-fw-v0.2.0-dev.2`
- App：1,112,000 B
- App SHA-256：`8bbe009343813048d1388b9677978a88d5ea6b516f36feab96cc9491a1e0c188`
- manifest：`dirty=false`，target=`esp32s3`，Flash 文件数=`4`

**发布产物**：
- `releases\luoye-fw-v0.2.0-dev.2-flash.zip`
- Flash ZIP SHA-256：
  `ad492f324050d0792bbdaaaddd35e3c77465bf7754840832907689fe0daae793`
- `releases\luoye-fw-v0.2.0-dev.2-symbols.zip`
- Symbols ZIP SHA-256：
  `d31ec0dd3f658c86da26635a05f1809e5df86a370a00da88ac603b65cd15b296`

**结论**：
- `dev.2` 可以烧入专用测试板验证完整启动。
- 尚未完成真板启动和 SD 故障注入，因此不得标记 `BOARD_PASS`。

**下一步**：
- 烧录后核对 `version=0.2.0-dev.2 commit=01d0d367dee5 dirty=0`。
- 串口必须先后出现 `LY|STACK ... after_ui`、`LY|STACK ... after_storage`、
  `LY|BOOT_READY` 和 `LY|STACK ... boot_ready`。

### 2026-07-31 00:40 — 启动 v0.3.0 SoftAP 配网和账号绑定

**版本**：`0.3.0-dev.1`
**阶段变化**：PLANNED → IN_PROGRESS → BUILD_PASS / SERVER_BLOCKED

**产品决策**：
- 固定式 SD 不再显示插卡、拔卡、重新插卡、更换卡或检查卡座提示。
- SoftAP 页面只接收 WiFi，不接收账号、账号密码、用户 ID 或服务器地址。
- 用户在 ClearMeeting 登录并用一次性配对码认领设备。
- 未获得可撤销 Device Token 前，固件禁止上传会话和音频。

**设备端代码**：
- BACK 长按 3 秒启动 `LUOYE-XXXX` SoftAP，密码每次随机生成。
- `192.168.4.1` 提供 WiFi 扫描、SSID/密码提交和连接状态接口。
- 候选凭据获得 IP 后才写入 NVS；错误密码不覆盖已验证配置。
- 设备 ID 从 MAC 后四位升级为完整 eFuse MAC 工程 ID。
- 实现 pair code/nonce 请求、状态轮询、Device Token 和脱敏账号保存。
- 墨水屏实现 AP、连接中、WiFi 已连接、待认领、已绑定和错误页面。
- 存储错误统一显示“存储不可用/录音未保存”等固定式存储文案。

**自动测试**：
- 状态机回归：通过，43 次渲染请求，包含配网状态迁移。
- 存储格式与断电尾部测试：通过。
- 配网页表单测试：通过，覆盖 UTF-8 SSID、URL 解码和密码长度边界。
- 静态安全检查：通过，确认固定 HTTPS、无账号输入框、日志不引用敏感值、
  UI 不含可插拔 SD 指令。
- ESP-IDF v5.5.4 增量构建：通过；App 约 1.19 MiB。

**线上服务器核验**：
- `http://clearmeeting.chat:34567/` 当前 Web 可访问。
- `/api/build-info` 返回 404。
- 当前 Web bundle 只有 auth、meetings、agenda 等接口，没有 device pair/claim。
- HTTPS 固件入口、Device Token、账号 Claim、解绑和设备列表尚未实现。

**契约**：
- 新草案：`luoye-device-api/0.2-draft`。
- 设计文档：`docs/PROVISIONING_AND_BINDING_DESIGN.md`。
- 设备端已按草案实现，但在服务器补齐前不得宣称账号绑定端到端完成。

**下一步**：
- 提交设备端源码后执行一次 `dirty=0` 全量构建并生成 SoftAP 真板测试包。
- 取得线上服务器源码、数据库 schema 和部署权限，实现 pair/claim/build-info。
- 真板验证热点可见、网页可打开、WiFi 可保存并在复位后自动重连。

### 2026-07-31 17:04 — v0.3.0-dev.1 SoftAP 工程包冻结

**版本**：`0.3.0-dev.1`
**阶段变化**：BUILD_PASS → READY_TO_FLASH / SERVER_BLOCKED

**版本组合**：
- 源码 commit：`ab837325dcd0e6720399d3bca3eaa67d3d88de07`
- annotated tag：`luoye-fw-v0.3.0-dev.1`
- API contract：`luoye-device-api/0.2-draft`
- server release：`unbound`
- build profile：`dev`
- target：`esp32s3`
- manifest：`dirty=false`

**干净构建**：
- ESP-IDF：`v5.5.4`
- App：1,215,680 B
- App SHA-256：
  `c1541ab51aea21c09764a157f12d55f74a71971053446cf2bb899a3b87b0ed2a`
- factory 6 MiB 分区剩余约 81%。

**发布产物**：
- `releases\luoye-fw-v0.3.0-dev.1-flash.zip`
- Flash ZIP SHA-256：
  `3d4f61e637c512813c14f6a5efa64624491c34583c2ce3df65e87b084593bf7c`
- `releases\luoye-fw-v0.3.0-dev.1-symbols.zip`
- Symbols ZIP SHA-256：
  `61bd997b1a75920160b14b695b6785c5a170867c50a53c5fc587a0b13a443e41`
- 解包后 manifest、版本、commit、API、target 和 4 个 BIN 的大小/哈希全部复验通过。

**允许验证**：
- 长按 BACK 进入配网。
- 随机密码 SoftAP 可见。
- `192.168.4.1` 页面、WiFi 扫描、错误密码重试、正确密码连接。
- 复位后使用已验证凭据自动连接。
- 固定式 SD 文案和既有录音功能无回归。

**不允许宣称**：
- 账号绑定端到端完成。
- Device Token 已由线上服务器签发。
- HTTPS 服务器和跨账号隔离已通过。

### 2026-07-31 19:10 — v0.4.0 持久离线补传设备端完成

**版本**：`0.4.0-dev.1`
**阶段变化**：PLANNED → IN_PROGRESS → BUILD_PASS / SERVER_BLOCKED

**设备端能力**：
- 遍历固定式 SD 上所有已闭合/已恢复会话，不再只跟踪 RAM 中最后一段录音。
- `upload.state` 原子持久化远端会话、分片序号、ACK 偏移、marks ACK、final ACK、
  重试次数和 HTTP 状态，并镜像到 `session.json`。
- create、160 KiB 音频分片、marks 和 final 各自使用稳定幂等键。
- 分片携带 client session、seq、offset、长度和 SHA-256；ACK 不精确匹配则不推进。
- 断网、超时、429 和 5xx 指数退避并加入抖动；401/403 清除失效 Token，保留录音。
- WiFi 获得 IP 与云 API/TLS 可用状态分离显示。
- 账号 binding generation 固化到新会话，旧账号积压不会自动转移给新账号。
- `done` 后保留 WAV、marks、manifest，不自动删除固定式 SD 数据。

**自动测试**：
- 状态机回归：通过（46 次渲染）。
- WAV/JSONL 恢复测试：通过。
- SoftAP 表单测试：通过。
- 上传协议测试：通过，覆盖整 160 KiB 边界、ACK 校验、幂等键、HTTP 分类与退避。
- provisioning/cloud-sync 静态门禁：通过。

**首次 ESP-IDF 构建**：
- ESP-IDF：`v5.5.4`
- target：`esp32s3`
- App：1,225,664 B
- App SHA-256：`8e6770e68396802250b7a80379fa09d8b9aad8c929d6d447c2b3e6952dbaa82b`
- factory 6 MiB 分区剩余约 81%。
- 该构建来自未提交工作树，只作为编译证据；发布包必须在 commit 后重新全量构建。

**外部阻塞**：
- `clearmeeting.chat` 尚未实现 `luoye-device-api/0.3-draft`。
- 当前不能完成 Device Token、账号隔离、服务端去重、断点续传和 final 的端到端验证。

**下一步**：
- 提交源码，执行 `dirty=0` 全量构建并生成 dev.1 FLASH/symbols 包。
- 真板先验证离线多段录音、复位恢复和本地队列；服务器完成后再做网络故障注入。

### 2026-07-31 19:12 — v0.4.0-dev.1 开发包冻结

**版本**：`0.4.0-dev.1`
**阶段变化**：BUILD_PASS / SERVER_BLOCKED → READY_TO_FLASH / SERVER_BLOCKED

**版本组合**：
- 源码 commit：`483931e6eac4e01446529c2b246381d1e6b3d8e3`
- annotated tag：`luoye-fw-v0.4.0-dev.1`
- API contract：`luoye-device-api/0.3-draft`
- build profile：`dev`
- manifest：`dirty=false`，target=`esp32s3`，Flash 文件数 `4`

**发布级全量构建**：
- ESP-IDF：`v5.5.4`
- App：1,225,664 B
- App SHA-256：`1e16cd37529f2ee3ef200d10e05f370f724ec7fdacb5ebd5e6f1d9e0c41e0ba0`
- factory 6 MiB 分区剩余约 81%。

**发布产物**：
- `releases\luoye-fw-v0.4.0-dev.1-flash.zip`
- Flash ZIP SHA-256：
  `30f3018acbe6268fac03f9605a7f12bc10ba67d1a0b373ec296b6cae0cf175df`
- `releases\luoye-fw-v0.4.0-dev.1-symbols.zip`
- Symbols ZIP SHA-256：
  `207d8cf5b5cdbd7914bef0db5dea7d94ac52392f1556835ce2d4b1450f0e24b2`

**结论**：
- 设备端固件可烧录并验证离线多会话、重启恢复和持久队列。
- 服务器 API 尚未实现，因此本状态不代表账号绑定、服务端去重或端到端补传已通过。
- 真板验收前不得标记 `BOARD_PASS`；服务器联调通过前不得标记 `ACCEPTED`。

### 2026-07-31 20:40 — v0.5.0 实时字幕、翻译和完整屏幕交互设备端完成

**版本**：`0.5.0-dev.1`
**阶段变化**：PLANNED → IN_PROGRESS → BUILD_PASS / SERVER_BLOCKED

**设备端能力**：
- 活动录音只从 SD 最近一次 `fsync` 边界读取，并只发送完整 160 KiB 分片；闭合后发送尾片。
- 轮询 `/live?after_revision=N`，严格校验 client/server session、revision 和连续 PCM 偏移。
- `result_revision`、`result_pcm_bytes` 原子持久化，断网恢复后从正确游标继续。
- 会议字幕与翻译原文/译文使用加锁 RAM 快照，正文不进入工程日志。
- 会议两页、翻译三页、充电页、真实空议程页、状态页、配网页、收尾页和错误页完成。
- 纯正文约 15 秒刷新，按键、暂停、结束、断网、配对和错误立即刷新。
- UI 不再使用 HTML 演示字幕；没有服务器结果时只显示等待、离线或本地保存事实。

**自动测试**：
- 状态机回归：通过（55 次渲染，含翻译三页循环）。
- 上传协议、实时游标/UTF-8/长度边界测试：通过。
- storage、provisioning、cloud-sync 与 live UI 静态门禁：通过。

**首次 ESP-IDF 构建**：
- ESP-IDF：`v5.5.4`
- target：`esp32s3`
- App：1,230,704 B
- App SHA-256：`40d0fed728c6b980f6f894fe26f5d96e7f6aa3458ed768e2bede5da82370d945`
- factory 6 MiB 分区剩余约 80%。
- 该构建来自未提交工作树，只作为编译证据；发布包必须在 commit 后重新全量构建。

**外部阻塞**：
- `clearmeeting.chat` 尚未实现 `luoye-device-api/0.4-draft`。
- 当前不能验证真实 Device Token、live revision、ASR/翻译、客户端活动会话和账号隔离。

**下一步**：
- 完成全部回归、提交源码，在 `dirty=0` 状态重新全量构建并冻结 dev.1 工程包。
- 服务器实现 0.4-draft 后执行断网、乱序 revision、偏移回退和双语一致性真板测试。

### 2026-07-31 20:08 — v0.5.0-dev.1 开发包冻结

**版本**：`0.5.0-dev.1`
**阶段变化**：BUILD_PASS → READY_TO_FLASH / SERVER_BLOCKED

**可追溯构建**：
- 源码 commit：`1f642c45b5cc64eedc2c736692464bec826c83b0`
- 构建时工作树：`dirty=false`
- API contract：`luoye-device-api/0.4-draft`
- ESP-IDF：`v5.5.4`，target `esp32s3`，profile `dev`
- 全量 clean build 与全部工程门禁：通过
- App：1,230,704 B
- App SHA-256：`46ebbdf3c0670186574925babb248a1618ba739613459553c06f5632240b5c33`

**发布产物**：
- `releases\luoye-fw-v0.5.0-dev.1-flash.zip`
  - SHA-256：`a1b48c982cc1fe0ce61b1fa79d3f8457af879c56f1d6266ba1ad38011c5482bf`
- `releases\luoye-fw-v0.5.0-dev.1-symbols.zip`
  - SHA-256：`ea6e48059f760be8466756e9c8ec77c114db15cd813154f69951b3fcaf035644`

**限制**：
- 包可烧录并验证设备 UI、录音与离线状态，但真实字幕/翻译仍依赖服务器实现 0.4-draft。
- 真板验收前不得标记 `BOARD_PASS`；服务端联调前不得标记 `ACCEPTED`。

### 2026-07-31 20:20 — v0.5.0-dev.2 全部刷新统一 FAST

**版本**：`0.5.0-dev.2`
**阶段变化**：READY_TO_FLASH → READY_TO_FLASH / SERVER_BLOCKED

- 所有 UI 实际渲染统一调用 SSD1680 厂商 FAST 波形 `0xC7`。
- 不再调用全刷 `0xF7` 或局刷 `0xFF`；`PART/FULL` 仅作为事件优先级标记保留。
- 适用于开机、配网、状态、录音、字幕、翻译、按键和收尾页面。
- 取舍：更新更快、无全刷闪烁；长时间运行残影风险增加，后续可由用户触发全刷维护。
- clean build、主机测试和静态门禁：通过。
- App：`0x12c590`（1,230,224 B）。
- FLASH SHA-256：`c525464e23b5ded1fb50c2ef6430ef24ec5eb435ef18547f751e81f8d6623e14`。

### 2026-07-31 23:26 — v0.6.0 议程、提醒和语音待办工程包冻结

**版本**：`0.6.0`
**阶段变化**：CODE_COMPLETE / SERVER_BLOCKED → READY_TO_FLASH / SERVER_BLOCKED

**设备端能力**：
- SNTP、系统 UTC 和 PCF8563 双向校时；RTC 闹钟调度最近提醒。
- revision/generation 议程同步、原子 SD 缓存、三页待机 UI 和提醒 REC/MARK/BACK 操作。
- MARK 最长 30 秒单声道语音待办；离线持久化、幂等上传、结果轮询和创建确认。
- 语音采集每 64 KiB 同步，异常重启修复 WAV 头并恢复待上传状态。
- 账号 binding generation 隔离；旧账号议程和待办不会进入新账号链路。
- 有提醒时使用 GPIO light sleep 等待 GPIO41，未设提醒时继续 deep sleep。

**可追溯构建**：
- 源码 commit：`a6541d018beb8335ee9ed01f23dc2858d6e61904`
- annotated tag：`luoye-fw-v0.6.0`
- 构建时工作树：`dirty=false`
- API contract：`luoye-device-api/0.5-draft`
- ESP-IDF：`v5.5.4`，target `esp32s3`，profile `rc`
- App：1,221,872 B，SHA-256：
  `9d35e710651f57be1b91d607c4ee96c537d604c0b7115d13b9e6ff99617d0a38`
- factory 6 MiB 分区剩余约 81%。

**自动门禁**：
- 状态机回归通过（72 次渲染）。
- WAV/JSONL 存储恢复、SoftAP 表单、上传协议、实时结果和议程协议测试全部通过。
- provisioning、cloud sync、live UI、agenda/todo 静态门禁全部通过。
- RC clean build、manifest/commit/版本/分区和 ZIP 解包复验通过。

**发布产物**：
- `releases\luoye-fw-v0.6.0-flash.zip`
  - SHA-256：`336ea7b9d94bb671bdfe484a0de086e99dc9ebdaa9bbffbcaf3fa8d9db3660b4`
- `releases\luoye-fw-v0.6.0-symbols.zip`
  - SHA-256：`76bd51d43add80311c3f9accbad57cedd83717a834cae7724d2fd89a63cb4728`

**限制**：
- 当前 `clearmeeting.chat` 尚未实现 0.5-draft，真实议程、ASR、时间解析和账号日历写入
  不能在本版本冻结时做端到端验证。
- 未烧录、未执行 PCF8563 到点唤醒和语音待办真板专项测试，因此不能标记 `BOARD_PASS`。

### 2026-08-01 01:38 — v0.6.1-cloud-v1 设备端 API v1 构建通过

**版本**：`0.6.1-cloud-v1`

**阶段变化**：`IN_PROGRESS → BUILD_PASS / API_V1_INTEGRATION`

**目标**：把设备端统一到 `luoye-device-api/1`，并冻结可供三端联调的协议实现。

**代码修改**：
- 服务端地址改为构建期配置；默认 `https://clearmeeting.chat`，明文 HTTP 仅允许
  `dev/engineering` 显式启用。
- 完成 build-info 兼容门禁、设备配对 start/status、同账号令牌轮换与结构化配对错误恢复。
- 完成 session/create、连续分片 ACK、mark、end、state、断网补传和幂等键 v1 契约。
- 完成议程与语音待办 v1 契约；待办截止时间允许 `null`。
- 普通对象 404 不再关闭全局云兼容门禁；只有 WiFi 或 build-info 门禁改变
  `cloud_ready`。
- 会话服务端状态冻结为 `uploading → processing → done|failed`；本地兼容读取历史
  `complete`，但新状态只写 `done`。

**自动测试**：
- 6 组主机测试通过：state、storage、provisioning、upload、live、agenda。
- 4 组静态门禁通过：provisioning、cloud sync、live UI、agenda/todo。
- `git diff --check` 通过。

**构建**：
- ESP-IDF：`v5.5.4`，target `esp32s3`，profile `dev`。
- 服务器 origin：`https://clearmeeting.chat`；`LUOYE_ALLOW_INSECURE_HTTP=OFF`。
- 全新目录：`build-api-v1-final`。
- App：1,274,288 B（`0x1371b0`），SHA-256：
  `a43353ef218c27790db17f176a4370654bf22857e13b4224c5dc89205c2adf74`。
- factory 6 MiB 分区剩余 `0x4c8e50`（80%）。

**边界**：
- 本次只形成设备端编译和契约门禁证据；没有打包、烧录、创建标签或发布。
- `BUILD_PASS` 不代表配对、上传、实时结果、议程和待办已经完成真服务器端到端验收。

每次工作追加一节，禁止覆盖旧记录：

```markdown
### YYYY-MM-DD HH:mm — 简短标题

**版本**：
**阶段变化**：IN_PROGRESS → CODE_COMPLETE
**目标**：
**证据等级**：线上探测确认 / 线上端到端确认 / 部署源码确认 / 本地旧源码参考 / 待确认

**版本组合**：
- 固件 commit / bin SHA-256：
- API contract 版本 / SHA-256：
- 服务器 commit / 镜像 digest / release ID：
- Web/App 版本：
- 数据库 migration：
- ASR / 翻译模型版本：

**代码修改**：
- 文件：
- 变更：

**自动测试**：
- 命令：
- 结果：

**构建**：
- ESP-IDF：
- Commit：
- App大小：
- SHA-256：

**烧录**：
- 设备编号：
- PCB版本：
- COM口：
- 结果：
- 串口日志：

**真板验收**：
- 通过：
- 失败：

**问题/风险**：
- ID：
- 复现：
- 决策：

**下一步**：
```

### 2026-08-04 17:25 — 1.54 寸最终 UI r2 接入

**版本**：`v0.7.0-ui154-r2`
**阶段变化**：IN_PROGRESS → BUILD_PASS
**目标**：将确认的 22 页 200×200 UI 布局接入 GDEY0154D67 固件，不改变录音、云端、按键和充电业务。

**代码修改**：
- `tools/gen_ui154_assets.py`：从无文字背景和最终 JSON 生成 22 个 1-bit 页面资源。
- `tools/gen_ui154_layout.py`：生成 130 个固件动态文字字段。
- `main/ui_render.c`：按布局字段绑定时间、电量、日程、字幕、录音、配网和待办数据。
- `main/ui_font.c`：支持按 JSON 像素字号缩放固件字库。

**自动测试**：
- BQ25186、议程待办、云同步、配网、实时字幕和 UI154 静态门禁通过。
- 22 个页面资源均为 5000 B，总计 110000 B。
- 固件字库真实预览：`docs/UI154_FIRMWARE_R2_PREVIEW.png`。

**构建**：
- ESP-IDF：v5.5.4-3
- Build：`build-154-ui`
- App：`0x12db20` B；factory 分区剩余 80%。

**边界**：
- 已完成编译和资源校验；仍需烧录后核对真屏文字细度、方向和 22 页状态切换。

### 2026-08-05 — SSD1681 深睡后差分 RAM 修复

**版本**：`v0.7.0-ui154-r2.1-epdfix`
**问题**：首帧后进入深睡，下一次 FAST 刷新没有重载旧帧 RAM2，导致屏幕显示随机噪点。
**修复**：ESP32 保存上一帧；每次唤醒后向 `0x26` 写入上一帧、向 `0x24` 写入当前帧，再执行 `0xC7` 快刷；BUSY 超时记为 `ESP_ERR_TIMEOUT`。
**验证**：UI154/BQ 静态门禁通过，ESP-IDF v5.5.4-3 编译通过，烧录包逐文件 SHA-256 校验通过。
**烧录包**：`luoye-fw-v0.7.0-ui154-r2.1-epdfix-flash.zip`。

### 2026-08-06 — 基于 r2.11 重做手动同步队列

**版本**：`v0.7.0-ui154-r2.11-syncqueue-r1`
**阶段变化**：SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH

**实现**：
- 完整移除后续实验版的 32 KiB 分片、持久前台会话、固定服务器路由和 60 秒 idle light-sleep 策略，回到 r2.11 的黑白 FAST、160 KiB 分片与常规联网基线。
- 当前正在录制的会话仍在线实时上传并获取字幕；已结束历史录音不再自动抢占上传通道。
- 待机长按中间键进入同步确认页；短按中间键后按最早优先 FIFO 上传，BACK 可取消确认或返回主页。
- 云端 final ACK 持久化后立即原子删除对应本地目录；网页删除任意已闭合本地录音不再要求上传完成。
- 删除正在写入的会话会被设备拒绝，避免破坏 FAT/WAV；闭合后的任意上传阶段均可删除。

**验证**：
- ESP-IDF v5.5.4-3 `build-dev` 全量编译通过；最终 App `0x12e280` B，6 MiB 应用分区剩余 80%。
- 状态机、存储格式、上传协议、实时结果、议程和配网 6 组主机测试通过。
- UI154、cloud sync、live UI、agenda/todo 和 manual-sync/FIFO/delete 静态门禁通过。
- ClearMeeting 设备 API 16 项测试和网页 5 项测试通过，网页生产构建通过。
- 固件烧录包：`luoye-fw-v0.7.0-ui154-r2.11-syncqueue-r1-flash.zip`。
- 配网 `LUOYE-XXXX` SoftAP 改为开放网络；墨水屏显示“无需密码”，目标 WiFi 自身的密码仍按网络实际情况填写。

**真板待验**：
- 中间键确认/取消、两场历史录音 FIFO 顺序、final ACK 后 SD 目录消失。
- 云端删除未上传/上传中/已上传的已闭合会话，以及断网和重启后的幂等恢复。

### 2026-08-07 — v0.8.0 单网络调度器与 API/2

**版本**：`v0.8.0-engineering-api2-r1`
**阶段变化**：SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH

**实现**：
- 所有已认证云业务收敛到一个网络调度任务；在线录音、收尾、手动历史同步、
  待办、议程和存储命令按状态门禁与优先级运行。
- 在线音频保持 160 KiB；完全离线和断网补洞采用 10 MiB 逻辑范围，以 64 KiB
  RAM 缓冲流式发送，不提供 2 MiB 降级。
- `upload.state` 持久化 `live/bulk/repair` 与服务器确认进度；缺口补齐、MARK 成功、
  complete 最终 ACK 后才删除 SD 历史副本。
- 网页删除不完整上传时先取消远端临时会话；录音中的删除延迟到安全收尾。
- 录音和收尾禁止休眠；已安全闭合的离线队列在联网失败 60 秒后可持久休眠。

**自动验证**：
- ESP-IDF v5.5.4 工程构建成功，应用镜像约 1.19 MiB，应用分区剩余约 80%。
- 固件主机测试和静态门禁全部通过。
- ClearMeeting 79 项服务器测试、6 项真实 HTTP API/2 联调、5 项网页测试和生产
  构建通过。

**真板待验**：
- 在线 160 KiB、纯离线 10 MiB、中途断网补洞与重启续传。
- 云端删除、完成 ACK 后 SD 删除、字幕时延、60 秒休眠及按键唤醒。

### 2026-08-10 — v0.9.0 三功能键交互基线

**版本**：`v0.9.0-engineering-three-key-r1`
**阶段变化**：SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH

**实现**：
- 录音键：短按录音/暂停/继续，录音中长按安全结束，待机长按进入历史同步。
- 待办键：待机短按进入并循环翻阅日程，长按开始待办、松开结束；确认页短按确认；录音中短按保留 MARK。
- 设置键：待机短按设备状态/返回，日程页短按返回主页，长按配网；录音中保留状态切换与锁定。
- 日程改为每页两条、时间一行加内容两行，显示当前页/总页数；修复旧选择器可能重复第一条的问题。
- 只合并用户批准的 `02_agenda` 页面，其余 21 页继续沿用已验证布局。

**已通过**：
- 状态机 PC 单元测试。
- UI154、agenda/todo、manual-sync 静态门禁。

**构建与发布**：
- ESP-IDF v5.5.4 全新构建通过；应用镜像 `0x1315b0` B，6 MiB 应用分区剩余 80%。
- 22 页 UI、SimSun 单色点阵字库和 assets 分区打包通过。
- 烧录包：`luoye-fw-v0.9.0-engineering-three-key-r1-flash.zip`。
- Flash ZIP SHA-256：`bea517726f965d7beda2af99e8c8d382004cb1fcff46083f9ce4850855cb7454`。

**待完成**：
- 真板验证三键短按/长按、待办松手收尾、日程循环分页和录音 MARK/锁定。

### 2026-08-11 — v0.9.1 双麦远场与 Wi-Fi 唤醒恢复

**版本**：`v0.9.1-engineering-dualmic-wifi-r2`
**阶段变化**：`SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 将双麦 A/B r3 前端接入正式会议 WAV 与语音待办：20 ms 帧、左右灵敏度平衡、±2 sample TDOA、分数延时叠加、高通、连续语音增益与软限幅。
- 保留正式驱动的左右声道诊断旁路；远场算法不改变 PDM 引脚、采样率、WAV 格式和 SD 写入协议。
- Wi-Fi 待机恢复改为异步三态：休眠、恢复中、取得 IP；只有 `IP_EVENT_STA_GOT_IP` 才清除休眠标志。
- 启动、扫描或连接失败每 5 秒重新扫描最多 8 个已保存 Wi-Fi；增加 connecting 门闩，禁止 `STA_START` 重复通知打断正在建立的连接。
- 本地录音不依赖云端恢复；录音、收尾、手动同步及正在执行的云请求均禁止再次进入休眠。

**验证**：
- ESP-IDF v5.5.4 全量构建及 connecting 门闩增量构建通过；应用镜像 `0x132c40` B，6 MiB 应用分区剩余 80%。
- 状态机 94 次渲染、WAV/断电恢复、配网、上传、实时结果、议程协议主机测试全部通过。
- provisioning、cloud-sync、agenda/todo、manual-sync/FIFO/delete、idle/light-sleep 静态门禁全部通过。
- 最终烧录包：`luoye-fw-v0.9.1-engineering-dualmic-wifi-r2-flash.zip`。
- Flash ZIP SHA-256：`64ef396c903466fab862db263eb0229a3881c53016294f2eeb796d8bb3912c22`。

**真板待验**：
- 60 秒待机后按 REC/MARK/BACK，确认 30 秒内依次出现 `resume_requested`、`radio_started`、`resumed result=ESP_OK`、`LY|WIFI`、`LY|CLOUD|ready=1`。
- 分别录制 0.5 m 与 3–5 m 人声，核对 `LY|AUDIO_FRONTEND|state=calibrated/stopped`、文件清晰度、底噪和削波。

### 2026-08-13 — v0.9.4 语义章节纪要与分钟刷新

**版本**：`v0.9.4-engineering-semantic-timeline-r1`
**阶段变化**：`SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 保留 v0.9.0 已确认的前 22 页，仅追加用户批准的第 23 页“本段纪要”；布局与素材
  SHA-256 均为 `8d6c8fa9d1c442b38a5416d8e6ceedf99d39ea836e7cb36ecde41574a354b5ba`。
- 页面显示本段起始时间、章节标题及两条动态要点；没有云端章节时继续使用字幕页，不阻断录音。
- 录音开始立即显示首帧；之后章节/字幕仅在自然分钟边界使用 SSD1681 FAST 刷新。
- 网络轮询和章节缓存继续后台运行；网络、云端及 SD 状态变化不会绕过分钟显示闸门。
- 暂停、继续、结束、错误及用户按键仍即时刷新。
- 接口保持 `luoye-device-api/2`，配套 ClearMeeting 服务器版本为 `0.19.0`。

**验证与发布**：
- UI154、实时 UI、配网、云同步、议程/待办、手动同步、休眠/EPD 静态门禁全部通过。
- 状态机 94 次渲染以及字幕、议程、存储、上传、配网协议测试全部通过。
- ESP-IDF v5.5.4 完整构建成功；应用镜像 `0x133ac0` B，6 MiB 应用分区剩余 80%。
- 烧录包：`luoye-fw-v0.9.4-engineering-semantic-timeline-r1-flash.zip`。
- Flash ZIP SHA-256：`c307ebbb8d8541a259e517b56c7749ce5ab248bf0c46a0eea56c2235a1f5a180`。

**真板待验**：
- 烧录后核对第 23 页方向、字体、两条要点和 FAST 波形。
- 录音跨越两个自然分钟，确认每分钟最多一次章节页自动刷新，暂停/结束仍立即变化。
- 同一议题持续讨论与明确切换议题各测试一次，确认章节数由语义变化决定，而不是固定时间决定。

### 2026-08-14 — v0.9.5 字幕/时间轴双视图与季度刷新

**版本**：`v0.9.5-engineering-caption-timeline-r1`
**阶段变化**：`SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 恢复独立实时字幕页；字幕使用 18px 宋体单色点阵、24px 行距，最多显示 6 行。
- 每场新会议默认进入实时字幕；录音中长按待办键可切换实时字幕和时间戳纪要。
- 当前显示选择在暂停、锁定和录音状态页之间保持；结束后新开会议自动回到实时字幕。
- 录音中短按待办键继续写入 MARK，短按设置键继续切换录音主显示和状态页，其他页面操作不变。
- 时间戳纪要改为字幕式排版，显示章节时间范围、18px 标题及两条动态要点。
- 活跃录音主显示在自然时钟 `:15`、`:30`、`:45` 使用黑白 FAST 刷新，在 `:00` 执行一次黑白 FULL 清屏；暂停、结束和用户切页仍即时响应。
- 接口保持 `luoye-device-api/2`，配套 ClearMeeting 服务器版本仍为 `0.19.0`。

**验证**：
- 状态机 98 次渲染回归通过，覆盖长按切换、暂停/锁定保持、MARK 与新会议复位。
- UI154、实时 UI、配网、云同步、议程/待办、手动同步、休眠/EPD 静态门禁全部通过。
- 字幕、议程、存储、上传、配网协议测试全部通过。
- ESP-IDF v5.5.4 完整构建成功；应用镜像 `1261056` B。
- 烧录包：`luoye-fw-v0.9.5-engineering-caption-timeline-r1-flash.zip`。
- Flash ZIP SHA-256：`65b904bd7806ed523b8c15bf71396d80b5a3116a7eb8f80352e12031c0abdaea`。

**真板待验**：
- 新会议确认默认字幕；录音中长按待办键反复切换字幕/时间戳纪要。
- 切换后依次测试暂停、锁定、状态页往返，确认所选视图不丢失；新会议确认回到字幕。
- 跨越自然分钟观察 `:15/:30/:45` FAST 与 `:00` FULL，确认无额外周期刷新。

### 2026-08-14 — v0.9.6 SSD1681 真局部窗口刷新

**版本**：`v0.9.6-engineering-caption-timeline-partial-r1`
**阶段变化**：`v0.9.5 FAST 代替局刷 → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 新增 SSD1681 原厂 `0xFF` PARTIAL 更新路径和逻辑坐标到屏幕 RAM 坐标的旋转/镜像换算。
- PARTIAL 只设置并写入录音主页面动态区域 `(4,8)-(195,178)`，不会再调用全屏 FAST 冒充局刷。
- FULL 与 FAST 后同步 SSD1681 RAM `0x26` 基准，避免随后的局部差分刷新花屏。
- `:15/:30/:45` 使用窗口 PARTIAL；`:00` 保持 FULL 清残影；用户切页仍保持全屏 FAST。
- v0.9.5 烧录包由本版本替代，不再作为真板测试候选。

**验证**：
- 状态机 98 次渲染以及 UI154、实时 UI、议程/待办和配网静态门禁通过。
- ESP-IDF v5.5.4 完整构建成功；应用镜像 `0x134160` B，应用分区剩余 80%。
- 烧录包：`luoye-fw-v0.9.6-engineering-caption-timeline-partial-r1-flash.zip`。
- Flash ZIP SHA-256：`03d7c8559b7b6425ae950c0ba67f62ca18c207c9097d0e198370226a9dce2ff0`。

### 2026-08-14 — v0.9.8 五秒局刷与两分钟 FAST

**版本**：`v0.9.8-engineering-partial5s-fast2m-r1`
**阶段变化**：`SPEC_CONFIRMED → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 延续 v0.9.7 的时间戳纪要字库修正，标题、项目符号和两条要点均使用完整的 16px SimSun 点阵字库。
- 录音字幕页和时间戳纪要页在自然时钟每 5 秒执行一次 SSD1681 真窗口 PARTIAL。
- 每 2 分钟执行一次全屏黑白 FAST，用于统一屏幕基准并抑制局刷残影。
- 删除所有运行期周期 FULL；页面切换、暂停、继续、结束和错误状态均使用 FAST。
- 仅屏幕上电后的首帧保留一次 FULL，以建立 SSD1681 差分刷新所需的基准 RAM。

**验证与发布**：
- 状态机 98 次渲染以及存储、上传、配网、实时字幕/纪要和议程协议测试全部通过。
- UI154、实时 UI、云同步、手动同步和休眠/EPD 静态门禁全部通过。
- ESP-IDF v5.5.4 完整构建成功；应用镜像 `0x134180` B，6 MiB 应用分区剩余 80%。
- 烧录包：`luoye-fw-v0.9.8-engineering-partial5s-fast2m-r1-flash.zip`。
- Flash ZIP SHA-256：`f90c604e3d0cef63e39be2147f71bb5abeb53216a7dffa783204de19950e48f9`。

### 2026-08-14 — v0.9.9 时间戳纪要字体修正

**版本**：`v0.9.9-engineering-timeline-font-r1`
**阶段变化**：`BUG_CONFIRMED → BUILD_PASS / READY_TO_FLASH`

**实现**：
- 章节大标题固定使用 18px SimSun 完整点阵字库。
- 顶部标签统一为“时间戳纪要”，从 12px 精简字库切换到 16px 完整字库。
- 16px 字库已确认包含 `U+6233 戳`，不再使用缺字方框代替。
- 保留 v0.9.8 的每 5 秒窗口 PARTIAL、每 2 分钟全屏 FAST 策略。

**验证与发布**：
- 时间戳字体门禁、UI154 静态检查和状态机 98 次渲染回归通过。
- ESP-IDF v5.5.4 完整构建成功；应用镜像 `0x134180` B。
- 烧录包：`luoye-fw-v0.9.9-engineering-timeline-font-r1-flash.zip`。
- Flash ZIP SHA-256：`eb4995241ab5b76e63891bd46ccc08600598a7d6995ae7d05c594db3aced699a`。
